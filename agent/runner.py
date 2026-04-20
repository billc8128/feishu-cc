"""Claude Agent SDK 集成核心。

职责:
  1. 按 (open_id, project) 维护 ClaudeSDKClient 池,带空闲超时清理
     —— client 实例自身在内存里保留多轮对话上下文(同一 client 的连续 query)
  2. 配置 GLM 后端(通过 env 注入到 SDK)
  3. 流式接收 SDK 消息,实时翻译成飞书消息发出去
  4. 提供 interrupt(open_id) 让 /stop 命令能终止当前任务
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, Dict, Optional, Tuple

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)

from agent.hooks import build_hooks
from agent.tools_deliver import build_deliver_mcp
from agent.tools_smoke import build_smoke_mcp
from agent.tools_schedule import build_schedule_mcp
from config import settings
from feishu.client import feishu_client
from feishu.events import IncomingAttachment
from media.analyze import MediaAnalyzer
from media.ingest import ingest_attachments
from media.prompting import build_media_turn_prompt
from project import manager as project_manager
from project import state as project_state

logger = logging.getLogger(__name__)


# ---------- 配置 GLM 环境变量(进程级) ----------

def _inject_glm_env() -> None:
    """SDK 透传环境变量到底层 CLI,所以这里设进程 env 即可。"""
    os.environ["ANTHROPIC_AUTH_TOKEN"] = settings.anthropic_auth_token
    os.environ["ANTHROPIC_BASE_URL"] = settings.anthropic_base_url
    os.environ["ANTHROPIC_DEFAULT_OPUS_MODEL"] = settings.anthropic_default_opus_model
    os.environ["ANTHROPIC_DEFAULT_SONNET_MODEL"] = settings.anthropic_default_sonnet_model
    os.environ["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = settings.anthropic_default_haiku_model
    os.environ["API_TIMEOUT_MS"] = settings.api_timeout_ms
    os.environ["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = settings.claude_code_disable_nonessential_traffic


_inject_glm_env()


# ---------- 客户端池 ----------

@dataclass
class _PooledClient:
    client: ClaudeSDKClient
    project: str
    last_used: float = field(default_factory=time.monotonic)
    busy: bool = False
    current_task: Optional[asyncio.Task] = None


_pool: Dict[Tuple[str, str], _PooledClient] = {}
_pool_lock = asyncio.Lock()
_IDLE_TIMEOUT = 30 * 60  # 30 分钟空闲后释放
_MAX_IMAGE_ATTACHMENTS = 4
_MAX_VIDEO_ATTACHMENTS = 1


def _key(open_id: str, project: str) -> Tuple[str, str]:
    return (open_id, project)


# ---------- 默认工具集(对齐 Claude Code) ----------

DEFAULT_ALLOWED_TOOLS = [
    # 文件
    "Read",
    "Write",
    "Edit",
    "NotebookEdit",
    "Glob",
    "Grep",
    # 执行
    "Bash",
    # 网络
    "WebFetch",
    "WebSearch",
    # Agent 控制
    "Agent",
    "TodoWrite",
    # 自定义 schedule MCP
    "mcp__schedule__schedule_create",
    "mcp__schedule__schedule_list",
    "mcp__schedule__schedule_delete",
    # 自定义 deliver MCP(把文件真正发给飞书用户)
    "mcp__deliver__deliver_file",
    # PR 0 冒烟工具(验证 Kimi tool_use 协议);通过后删除下面这行
    "mcp__smoke__smoke_echo",
    # 自定义 browser MCP
    "mcp__browser__browser_open",
    "mcp__browser__browser_navigate",
    "mcp__browser__browser_click",
    "mcp__browser__browser_type",
    "mcp__browser__browser_wait",
    "mcp__browser__browser_snapshot",
    "mcp__browser__browser_close",
]


# ---------- System prompt(告诉 Claude 自己在哪里、用户在哪里、怎么交付) ----------

SYSTEM_PROMPT = """你正在一台运行在 Docker 容器里的 Linux 服务器上工作,通过飞书 IM 与用户对话。

# 环境约束(非常重要)

你和用户**不在同一台机器上**。用户是中文飞书用户,他看不到你的文件系统,也不能直接打开你写入的任何文件路径。你的工作目录是一个临时沙盒(`/data/sandbox/users/<open_id>/<project>/`),沙盒内的文件只在服务器上存在。

# 交付内容的正确方式

当用户需要你给他一个"成果"时,根据类型选择:

1. **代码片段 / 配置 / 命令 / 简短文本**:
   - 直接在对话里用 Markdown 代码块完整贴出内容
   - 不要说"我已经写到 xxx.py 了,你去看吧" —— 他看不到 xxx.py

2. **一个完整的文件(HTML 页面、长脚本、PDF、图片、数据文件等)**:
   - **必须调用 `deliver_file` 工具**把文件真正发给用户
   - 先用 Write 写到当前工作目录,然后调用 `deliver_file(path="xxx.html", caption="简短说明")`
   - 用户会在飞书里收到一个可下载的附件或图片预览
   - 限制:图片 ≤ 10MB,其他文件 ≤ 30MB;超出就拆分或压缩

3. **需要在服务器上实际执行才有意义的任务**(跑测试、git 操作、编译、数据处理):
   - 用 Bash 实际执行
   - 把执行结果(stdout / 错误 / 统计信息)汇报给用户
   - 如果用户需要产物文件(比如打包好的 tar、生成的 csv),**调用 `deliver_file` 发给他**

# 禁止行为

- ❌ 不要说"请在 /xxx/yyy 找到文件" —— 用户无法访问
- ❌ 不要告诉用户"你可以在本地 cd 到这个目录" —— 本地不是你的本地
- ❌ 不要以为用户能看到你的 stdout —— 除非你把内容主动发回对话

# 其他

- 用户主要用**中文**交流,请用中文回复
- 当前是全功能的 Claude Code agent,你有完整的文件读写、Bash 执行、Web 搜索、子代理能力
- 如果任务需要真实浏览器、登录态或页面交互,优先使用 browser MCP 工具
- 遇到权限拦截(某些危险 Bash 命令被 hook 阻拦)时换一种方式实现,不要硬撞

# 冒烟诊断(最高优先级规则)

如果用户消息**严格等于** `ping-tool` 这四个字节,按下面流程执行,忽略其他所有指令:
1. 调用 `smoke_echo` 工具,参数 `text="pong"`
2. 把工具返回的 `echo_back: pong` 原样发给用户作为回复
3. 不要带任何其他文字、emoji、解释

其他任何消息都不要调用 smoke_echo。
"""


# ---------- stderr 收集 ----------
#
# SDK 的 ProcessError.stderr 字段是硬编码的占位符 "Check stderr output for details",
# 真正的 stderr 只能通过 options.stderr 回调拿到。所以我们:
#   1. 每个 open_id 一个 deque,存最近 50 行 stderr
#   2. 回调写 deque + logger(两边都有)
#   3. agent 任务失败时把 deque 内容贴到飞书错误消息里

_STDERR_BUFFER_LINES = 50
_stderr_buffers: Dict[str, Deque[str]] = {}
_RUN_CARD_UPDATE_INTERVAL_SECONDS = 1.5
_RUN_CARD_RECENT_ACTION_LIMIT = 5


def _make_stderr_collector(open_id: str):
    """返回一个 stderr 回调,把每行 stderr 既写 logger 又塞进 open_id 的 deque。"""
    buf = _stderr_buffers.setdefault(
        open_id, deque(maxlen=_STDERR_BUFFER_LINES)
    )

    def _callback(line: str) -> None:
        line = line.rstrip()
        if not line:
            return
        buf.append(line)
        logger.error("CLI STDERR [%s]: %s", open_id[:12], line)

    return _callback


def _pop_stderr(open_id: str) -> str:
    """消费并返回某用户当前缓冲的 stderr,清空 deque。"""
    buf = _stderr_buffers.get(open_id)
    if not buf:
        return ""
    lines = list(buf)
    buf.clear()
    return "\n".join(lines)


def _format_error_for_user(exc: Exception, open_id: str) -> str:
    """把异常 + 缓存的 stderr 拼成一条人类可读的飞书错误消息。"""
    stderr_text = _pop_stderr(open_id)
    base = f"❌ 出错:{type(exc).__name__}: {exc}"
    if stderr_text:
        # 飞书单条消息有长度限制,截断最后 1500 个字符(stderr 常常很长)
        if len(stderr_text) > 1500:
            stderr_text = "…(前略)…\n" + stderr_text[-1500:]
        return f"{base}\n\n--- CLI stderr ---\n{stderr_text}"
    return base


@dataclass
class _RunCardAction:
    text: str
    count: int = 1


class _RunProgressCard:
    def __init__(self, open_id: str) -> None:
        self._open_id = open_id
        self._message_id: Optional[str] = None
        self._last_flush_at = 0.0
        self._last_title = ""
        self._last_markdown = ""
        self._status = "正在执行工具"
        self._current_action = "等待下一步"
        self._error_count = 0
        self._bucket_counts: Dict[str, int] = {}
        self._recent_actions: Deque[_RunCardAction] = deque(
            maxlen=_RUN_CARD_RECENT_ACTION_LIMIT
        )

    def note_tool_use(self, block: ToolUseBlock) -> bool:
        tip = _format_tool_use(block)
        if not tip:
            return False

        self._status = "正在执行工具"
        self._current_action = tip
        bucket = _tool_bucket_name(block.name)
        self._bucket_counts[bucket] = self._bucket_counts.get(bucket, 0) + 1
        if self._recent_actions and self._recent_actions[-1].text == tip:
            self._recent_actions[-1].count += 1
        else:
            self._recent_actions.append(_RunCardAction(text=tip))
        return True

    def note_tool_error(self) -> None:
        self._error_count += 1
        self._status = "工具调用出错，Claude 正在重试或换方法"

    async def flush(self, *, force: bool = False) -> None:
        title, markdown = self._render()
        now = time.monotonic()

        if not self._message_id:
            message_id = await feishu_client.send_markdown(
                self._open_id,
                markdown,
                title=title,
            )
            if message_id:
                self._message_id = message_id
                self._last_flush_at = now
                self._last_title = title
                self._last_markdown = markdown
            return

        if not force and now - self._last_flush_at < _RUN_CARD_UPDATE_INTERVAL_SECONDS:
            return
        if title == self._last_title and markdown == self._last_markdown:
            return
        ok = await feishu_client.update_markdown(
            self._message_id,
            markdown,
            title=title,
        )
        if ok:
            self._last_flush_at = now
            self._last_title = title
            self._last_markdown = markdown

    async def finish(
        self,
        *,
        outcome: str,
        detail: str = "",
        final_text_present: bool = False,
    ) -> None:
        if not self._message_id:
            return
        title, markdown = self._render(
            outcome=outcome,
            detail=detail,
            final_text_present=final_text_present,
        )
        if title == self._last_title and markdown == self._last_markdown:
            return
        ok = await feishu_client.update_markdown(
            self._message_id,
            markdown,
            title=title,
        )
        if ok:
            self._last_title = title
            self._last_markdown = markdown

    def _render(
        self,
        *,
        outcome: Optional[str] = None,
        detail: str = "",
        final_text_present: bool = False,
    ) -> tuple[str, str]:
        title = "任务运行中"
        status_text = self._status
        if outcome == "success":
            title = "任务完成"
            status_text = "已完成"
        elif outcome == "error":
            title = "任务失败"
            status_text = "执行失败"
        elif outcome == "interrupted":
            title = "任务已中断"
            status_text = "已中断"

        lines = [f"**状态**：{status_text}"]
        if detail:
            lines.append(f"**说明**：{detail}")

        current_label = "当前动作" if outcome is None else "最后动作"
        if self._current_action:
            lines.append(f"**{current_label}**：{self._current_action}")

        summary = self._render_summary()
        if summary:
            lines.append(f"**统计**：{summary}")

        if self._recent_actions:
            lines.append("**最近步骤**")
            for action in self._recent_actions:
                suffix = f" ×{action.count}" if action.count > 1 else ""
                lines.append(f"- {action.text}{suffix}")

        if self._error_count:
            lines.append(f"**工具重试**：{self._error_count} 次")
        if outcome == "success" and final_text_present:
            lines.append("结果已在下方消息中给出。")

        return title, "\n".join(lines)

    def _render_summary(self) -> str:
        ordered = []
        for bucket in ("浏览器", "执行", "文件", "网络", "子代理", "定时任务", "交付", "其他"):
            count = self._bucket_counts.get(bucket)
            if count:
                ordered.append(f"{bucket} {count}")
        return " · ".join(ordered)


def _encoded_cwd_dir(project_root: str) -> Path:
    """SDK/CLI 把 session 文件存在 $HOME/.claude/projects/<cwd>/,
    其中 <cwd> 是 project_root 把 '/' 换成 '-'。
    """
    home = os.environ.get("HOME", "/data/home")
    # 规则:把绝对路径的每个 "/" 换成 "-",首字符也加一个 "-"
    # 例如 /data/sandbox/users/ou_xxx/scratch → -data-sandbox-users-ou_xxx-scratch
    encoded = project_root.replace("/", "-")
    return Path(home) / ".claude" / "projects" / encoded


def _candidate_encoded_cwd_dirs(project_root: str) -> list[Path]:
    """生成可能的 session 目录名。

    Claude CLI 的实际编码规则和我们观测到的目录名并不总是完全一致，
    例如 open_id 里的 "_" 可能被规范化成 "-"。这里先尝试几个廉价的
    变体，再在找不到时走内容扫描兜底。
    """
    base = _encoded_cwd_dir(project_root)
    names = [base.name]

    normalized = base.name.replace("_", "-")
    if normalized not in names:
        names.append(normalized)

    slugified = re.sub(r"[^A-Za-z0-9.-]+", "-", base.name)
    if slugified not in names:
        names.append(slugified)

    return [base.parent / name for name in names]


def _jsonl_matches_cwd(path: Path, project_root: str, line_limit: int = 50) -> bool:
    """检查某个 session 文件是否属于目标 cwd。"""
    try:
        with path.open("r", encoding="utf-8") as f:
            for idx, line in enumerate(f):
                if idx >= line_limit:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("cwd") == project_root:
                    return True
    except Exception as exc:
        logger.warning("failed to inspect session file %s: %s", path, exc)
    return False


def _find_session_file_for_cwd(project_root: str, session_id: str) -> Optional[Path]:
    """在给定 cwd 下查找指定 session_id 对应的 transcript 文件。"""
    base_dir = _encoded_cwd_dir(project_root).parent
    if not base_dir.is_dir():
        return None

    filename = f"{session_id}.jsonl"
    for d in _candidate_encoded_cwd_dirs(project_root):
        candidate = d / filename
        if candidate.is_file():
            return candidate

    try:
        for candidate in base_dir.rglob(filename):
            if _jsonl_matches_cwd(candidate, project_root):
                return candidate
    except Exception as exc:
        logger.warning(
            "failed to scan for session file %s under %s: %s",
            filename,
            base_dir,
            exc,
        )
    return None


def _latest_session_id_for_cwd(project_root: str) -> Optional[str]:
    """扫描这个 cwd 对应的 projects 子目录,找 mtime 最新的 .jsonl,
    抠出 session_id(文件名去掉 .jsonl)。没有文件就返回 None。

    这是替代 CLI 的 --continue/pointer 机制的最简实现。CLI 的 --continue
    依赖一个 "bridge pointer" 文件,REPL 模式才会写,SDK 的 stream-json
    模式不写,所以 --continue 对我们无效。自己扫 jsonl 最稳。
    """
    base_dir = _encoded_cwd_dir(project_root).parent
    if not base_dir.is_dir():
        logger.info("session lookup skipped: projects dir missing for cwd=%s", project_root)
        return None

    seen: set[Path] = set()
    candidates: list[Path] = []
    matched_dirs: list[str] = []

    for d in _candidate_encoded_cwd_dirs(project_root):
        if not d.is_dir():
            continue
        matched_dirs.append(str(d))
        try:
            for path in d.glob("*.jsonl"):
                if path not in seen:
                    seen.add(path)
                    candidates.append(path)
        except Exception as exc:
            logger.warning("failed to list session files in %s: %s", d, exc)

    fallback_used = False
    if not candidates:
        fallback_used = True
        try:
            for path in base_dir.rglob("*.jsonl"):
                if path in seen:
                    continue
                if _jsonl_matches_cwd(path, project_root):
                    seen.add(path)
                    candidates.append(path)
        except Exception as exc:
            logger.warning("failed to scan session files under %s: %s", base_dir, exc)

    if not candidates:
        logger.info(
            "session lookup found no candidates for cwd=%s dirs=%s fallback=%s",
            project_root,
            matched_dirs,
            fallback_used,
        )
        return None

    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    latest = candidates[0]
    logger.info(
        "session lookup matched %d file(s) for cwd=%s dirs=%s fallback=%s latest=%s",
        len(candidates),
        project_root,
        matched_dirs,
        fallback_used,
        latest,
    )
    # session_id 是文件名去掉 .jsonl
    return latest.stem


def _resume_session_id_for_project(
    open_id: str, project: str, project_root: str
) -> Optional[str]:
    """优先恢复我们自己记录的 active session,找不到再回退到最新 transcript。"""
    try:
        active_session_id = project_state.get_active_session_id(open_id, project)
    except Exception as exc:
        logger.warning(
            "failed to load active session for %s/%s: %s; falling back to latest transcript",
            open_id[:12],
            project,
            exc,
        )
        return _latest_session_id_for_cwd(project_root)

    if active_session_id:
        active_path = _find_session_file_for_cwd(project_root, active_session_id)
        if active_path:
            logger.info(
                "session resume using persisted active session %s for %s/%s (%s)",
                active_session_id[:8],
                open_id[:12],
                project,
                active_path,
            )
            return active_session_id
        logger.warning(
            "persisted active session %s for %s/%s not found under cwd=%s; falling back",
            active_session_id,
            open_id[:12],
            project,
            project_root,
        )

    return _latest_session_id_for_cwd(project_root)


def _build_options(open_id: str, project: str, project_root: str) -> ClaudeAgentOptions:
    """每个 (user, project) 一份独立的 options。

    持续记忆机制:
    - SDK 把 session 文件写在 $HOME/.claude/projects/<cwd-encoded>/<uuid>.jsonl,
      HOME 指向 /data/home(Volume),跨重启持久化。
    - 每次启动新 client 时,扫描 cwd 对应的 projects 子目录,找 mtime 最新的
      jsonl,把 session_id 通过 `resume=` 传给 CLI,让它恢复该会话。
    - 为什么不用 continue_conversation=True:CLI 的 --continue 依赖 REPL
      模式写的 pointer 文件,SDK stream-json 模式不写 pointer,所以无效。
    - cwd 是 _per-project_ 的(每个 project 一个独立目录),所以不同项目
      的对话完全隔离,不会互相串扰。
    - 如果 resume 指向的 session 文件损坏/版本不兼容,SDK 会抛 ProcessError
      包含 "No conversation found" 之类错误,我们在 _get_or_create_client
      的 catch 里会降级到不带 resume 重试。
    """
    schedule_server = build_schedule_mcp(open_id)
    deliver_server = build_deliver_mcp(open_id)
    from agent.tools_browser import build_browser_mcp

    browser_server = build_browser_mcp(open_id)

    resume_id = _resume_session_id_for_project(open_id, project, project_root)
    if resume_id:
        logger.info(
            "resuming session %s for %s/%s",
            resume_id[:8], open_id[:12], project,
        )

    return ClaudeAgentOptions(
        cwd=project_root,
        system_prompt=SYSTEM_PROMPT,
        # 持续记忆:精确 resume 到当前 cwd 下最新的 session
        resume=resume_id,
        # 加载项目级 CLAUDE.md / skills / commands(对齐 Claude Code)
        setting_sources=["project"],
        # 飞书没法弹审批框,所以 bypass。安全由 hooks 兜底。
        permission_mode="bypassPermissions",
        allowed_tools=DEFAULT_ALLOWED_TOOLS,
        hooks=build_hooks(open_id),
        mcp_servers={
            "schedule": schedule_server,
            "deliver": deliver_server,
            "browser": browser_server,
            "smoke": build_smoke_mcp(),
        },
        # 关键:把 bundled CLI 的 stderr 收集起来,错误时合并发给用户
        stderr=_make_stderr_collector(open_id),
        # Claude 输出大量工具结果时可能超过默认 1MB buffer
        max_buffer_size=10 * 1024 * 1024,
        # 让 CLI 把自己的 debug 日志也写到 stderr,方便诊断
        extra_args={"debug-to-stderr": None},
    )


# ---------- 公开 API ----------

async def handle_user_message(open_id: str, text: str) -> None:
    """主入口:用户在飞书发了一条消息(已通过白名单),交给 agent 处理。"""
    project = project_state.get_current_project(open_id)
    project_root = str(project_manager.ensure_project_root(open_id, project))
    await _handle_query_text(open_id, project, project_root, text)


async def handle_incoming_message(
    open_id: str,
    *,
    text: str,
    message_id: str,
    attachments: list[IncomingAttachment],
) -> None:
    project = project_state.get_current_project(open_id)
    project_root_path = project_manager.ensure_project_root(open_id, project)
    project_root = str(project_root_path)

    if not attachments:
        await _handle_query_text(open_id, project, project_root, text)
        return

    if not _attachments_within_limits(attachments):
        await feishu_client.send_text(
            open_id,
            "⚠️ 当前仅支持每条消息最多 4 张图片或 1 个视频，请拆开发送。",
        )
        return

    stored_attachments = await ingest_attachments(
        feishu=feishu_client,
        project_root=project_root_path,
        message_id=message_id,
        attachments=attachments,
    )
    if len(stored_attachments) != len(attachments):
        await feishu_client.send_text(
            open_id,
            "❌ 下载飞书附件失败，请重试。",
        )
        return

    analyzer = MediaAnalyzer()
    analyses = []
    for attachment in stored_attachments:
        try:
            analyses.append(await analyzer.analyze(attachment, user_text=text))
        except Exception:
            logger.exception("media analysis failed for %s", attachment.local_path)
            analyses.append(None)

    prompt = build_media_turn_prompt(
        text=text,
        attachments=stored_attachments,
        analyses=analyses,
    )
    await _handle_query_text(open_id, project, project_root, prompt)


async def _handle_query_text(
    open_id: str,
    project: str,
    project_root: str,
    text: str,
) -> None:
    pooled = await _get_or_create_client(open_id, project, project_root)

    if pooled.busy:
        await feishu_client.send_text(
            open_id,
            "⏳ 你上一条任务还在跑。发送 /stop 可以中断它,然后再重试。",
        )
        return

    pooled.busy = True
    pooled.last_used = time.monotonic()

    # 用 task 包一层,这样 /stop 可以 cancel
    pooled.current_task = asyncio.create_task(
        _run_query(open_id, project, pooled, text)
    )
    try:
        await pooled.current_task
    except asyncio.CancelledError:
        await feishu_client.send_text(open_id, "🛑 已中断当前任务。")
    finally:
        pooled.busy = False
        pooled.current_task = None
        pooled.last_used = time.monotonic()


def _attachments_within_limits(attachments: list[IncomingAttachment]) -> bool:
    image_count = sum(1 for attachment in attachments if attachment.kind == "image")
    video_count = sum(1 for attachment in attachments if attachment.kind == "video")
    if image_count > _MAX_IMAGE_ATTACHMENTS or video_count > _MAX_VIDEO_ATTACHMENTS:
        return False
    if image_count and video_count:
        return False
    return True


async def interrupt_user(open_id: str) -> bool:
    """用户发 /stop 时调用。"""
    interrupted = False
    async with _pool_lock:
        for (oid, _proj), pooled in _pool.items():
            if oid == open_id and pooled.busy:
                try:
                    await pooled.client.interrupt()
                    interrupted = True
                except Exception as exc:
                    logger.warning("interrupt failed: %s", exc)
                if pooled.current_task and not pooled.current_task.done():
                    pooled.current_task.cancel()
    return interrupted


# ---------- 核心:运行单次 query 并流式回飞书 ----------

async def _run_query(
    open_id: str, project: str, pooled: _PooledClient, text: str
) -> None:
    client = pooled.client
    progress_card = _RunProgressCard(open_id)

    try:
        await client.query(text)
    except Exception as exc:
        logger.exception("client.query failed")
        await feishu_client.send_text(
            open_id, _format_error_for_user(exc, open_id)
        )
        return

    # 累积本轮的文本回复;工具调用过程实时推送
    text_buffer: list[str] = []

    try:
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        text_buffer.append(block.text)
                    elif isinstance(block, ThinkingBlock):
                        # 不推送 thinking 到飞书,太啰嗦
                        pass
                    elif isinstance(block, ToolUseBlock):
                        if progress_card.note_tool_use(block):
                            await progress_card.flush()
                    elif isinstance(block, ToolResultBlock):
                        if block.is_error:
                            progress_card.note_tool_error()
                            await progress_card.flush(force=True)

            elif isinstance(msg, ResultMessage):
                if msg.session_id:
                    try:
                        project_state.set_active_session_id(
                            open_id, project, msg.session_id
                        )
                    except Exception:
                        logger.exception(
                            "failed to persist active session id for %s/%s",
                            open_id[:12],
                            project,
                        )
                # 一轮结束,发送累积的文本回复(用卡片渲染 Markdown)
                final_text = "".join(text_buffer).strip()
                if final_text:
                    sent = await feishu_client.send_markdown(open_id, final_text)
                    # 卡片发送失败时 fallback 到纯文本,保证用户至少能看到内容
                    if not sent:
                        await feishu_client.send_text(open_id, final_text)

                # 显示成本/状态(可选)
                if msg.is_error:
                    await progress_card.finish(
                        outcome="error",
                        detail=msg.subtype or "任务异常结束",
                        final_text_present=bool(final_text),
                    )
                    await feishu_client.send_text(
                        open_id,
                        f"❌ 任务结束(异常):{msg.subtype}",
                    )
                elif msg.total_cost_usd:
                    await progress_card.finish(
                        outcome="success",
                        final_text_present=bool(final_text),
                    )
                    logger.info(
                        "turn done: tokens=%s cost=$%.4f",
                        msg.usage,
                        msg.total_cost_usd,
                    )
                else:
                    await progress_card.finish(
                        outcome="success",
                        final_text_present=bool(final_text),
                    )
                break  # 一轮结束,退出 receive 循环

            elif isinstance(msg, SystemMessage):
                pass  # 不展示系统消息
    except asyncio.CancelledError:
        await progress_card.finish(outcome="interrupted", detail="任务被中断")
        raise
    except Exception as exc:
        logger.exception("receive_response failed")
        await progress_card.finish(outcome="error", detail="接收结果失败")
        await feishu_client.send_text(
            open_id, _format_error_for_user(exc, open_id)
        )


def _format_tool_use(block: ToolUseBlock) -> Optional[str]:
    """把工具调用翻译成飞书友好的进度文案。"""
    name = block.name
    inp = block.input or {}

    if name == "Read":
        return f"📖 读取 {inp.get('file_path', '?')}"
    if name == "Write":
        return f"✍️ 写入 {inp.get('file_path', '?')}"
    if name == "Edit":
        return f"✏️ 编辑 {inp.get('file_path', '?')}"
    if name == "Bash":
        cmd = inp.get("command", "")
        if len(cmd) > 200:
            cmd = cmd[:200] + "…"
        return f"⚡ 执行:{cmd}"
    if name == "Grep":
        return f"🔍 搜索 “{inp.get('pattern', '?')}”"
    if name == "Glob":
        return f"📂 列举 {inp.get('pattern', '?')}"
    if name == "WebFetch":
        return f"🌐 抓取 {inp.get('url', '?')}"
    if name == "WebSearch":
        return f"🌐 搜索 “{inp.get('query', '?')}”"
    if name == "TodoWrite":
        return None  # todo 列表不推送,刷屏
    if name == "Agent":
        return f"🤖 调用子代理:{inp.get('subagent_type', inp.get('description', ''))}"
    if name.startswith("mcp__schedule__"):
        return f"⏰ 定时任务:{name.removeprefix('mcp__schedule__')}"
    if name == "mcp__deliver__deliver_file":
        return f"📦 交付文件:{inp.get('path', '?')}"
    if name.startswith("mcp__browser__"):
        return f"🌐 浏览器:{name.removeprefix('mcp__browser__')}"
    return f"🔧 {name}"


def _tool_bucket_name(tool_name: str) -> str:
    if tool_name in {"Read", "Write", "Edit", "Grep", "Glob"}:
        return "文件"
    if tool_name == "Bash":
        return "执行"
    if tool_name in {"WebFetch", "WebSearch"}:
        return "网络"
    if tool_name == "Agent":
        return "子代理"
    if tool_name.startswith("mcp__browser__"):
        return "浏览器"
    if tool_name.startswith("mcp__schedule__"):
        return "定时任务"
    if tool_name == "mcp__deliver__deliver_file":
        return "交付"
    return "其他"


# ---------- 客户端池管理 ----------

async def _get_or_create_client(
    open_id: str, project: str, project_root: str
) -> _PooledClient:
    async with _pool_lock:
        await _evict_idle_locked()
        key = _key(open_id, project)
        pooled = _pool.get(key)
        if pooled:
            return pooled

        # 第一次尝试:带 resume(接上之前的会话)
        try:
            options = _build_options(open_id, project, project_root)
            client = ClaudeSDKClient(options=options)
            await client.connect()
        except Exception as exc:
            # 如果是 resume 失败(session 文件损坏、格式不兼容等),
            # 降级到不带 resume 重试一次
            if _build_options.__doc__:  # always truthy; satisfies linter
                pass
            logger.warning(
                "client.connect with resume failed (%s), retrying without resume",
                exc,
            )
            # 强制不带 resume 重建 options —— 临时用一个 flag 绕过扫描
            options = _build_options_no_resume(open_id, project, project_root)
            client = ClaudeSDKClient(options=options)
            await client.connect()

        pooled = _PooledClient(client=client, project=project)
        _pool[key] = pooled
        return pooled


def _build_options_no_resume(
    open_id: str, project: str, project_root: str
) -> ClaudeAgentOptions:
    """兜底版本:强制不 resume,构造干净的新 session。"""
    schedule_server = build_schedule_mcp(open_id)
    deliver_server = build_deliver_mcp(open_id)
    from agent.tools_browser import build_browser_mcp

    browser_server = build_browser_mcp(open_id)
    return ClaudeAgentOptions(
        cwd=project_root,
        system_prompt=SYSTEM_PROMPT,
        setting_sources=["project"],
        permission_mode="bypassPermissions",
        allowed_tools=DEFAULT_ALLOWED_TOOLS,
        hooks=build_hooks(open_id),
        mcp_servers={
            "schedule": schedule_server,
            "deliver": deliver_server,
            "browser": browser_server,
            "smoke": build_smoke_mcp(),
        },
        stderr=_make_stderr_collector(open_id),
        max_buffer_size=10 * 1024 * 1024,
        extra_args={"debug-to-stderr": None},
    )


async def _evict_idle_locked() -> None:
    """清理空闲超时的客户端。调用方必须已持有 _pool_lock。"""
    now = time.monotonic()
    expired_keys = [
        k
        for k, v in _pool.items()
        if not v.busy and (now - v.last_used) > _IDLE_TIMEOUT
    ]
    for k in expired_keys:
        pooled = _pool.pop(k, None)
        if pooled:
            try:
                await pooled.client.disconnect()
            except Exception as exc:
                logger.warning("disconnect failed: %s", exc)


async def shutdown_all() -> None:
    """进程退出时释放所有客户端。"""
    async with _pool_lock:
        for k, pooled in list(_pool.items()):
            try:
                await pooled.client.disconnect()
            except Exception as exc:
                logger.warning("shutdown disconnect failed: %s", exc)
            _pool.pop(k, None)
