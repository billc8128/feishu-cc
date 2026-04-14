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
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
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
from agent.tools_schedule import build_schedule_mcp
from config import settings
from feishu.client import feishu_client
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
- 遇到权限拦截(某些危险 Bash 命令被 hook 阻拦)时换一种方式实现,不要硬撞
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


def _build_options(open_id: str, project: str, project_root: str) -> ClaudeAgentOptions:
    """每个 (user, project) 一份独立的 options。

    注意:不传 resume —— ClaudeSDKClient 实例本身在内存里保留多轮对话上下文
    (只要 pool 里的 client 没过期)。跨客户端实例不保留历史,这是可接受的
    trade-off:SDK 的 session 文件生命周期不可控,强行 resume 会遇到
    'No conversation found' 崩溃。
    """
    schedule_server = build_schedule_mcp(open_id)
    deliver_server = build_deliver_mcp(open_id)

    return ClaudeAgentOptions(
        cwd=project_root,
        system_prompt=SYSTEM_PROMPT,
        # 加载项目级 CLAUDE.md / skills / commands(对齐 Claude Code)
        setting_sources=["project"],
        # 飞书没法弹审批框,所以 bypass。安全由 hooks 兜底。
        permission_mode="bypassPermissions",
        allowed_tools=DEFAULT_ALLOWED_TOOLS,
        hooks=build_hooks(open_id),
        mcp_servers={
            "schedule": schedule_server,
            "deliver": deliver_server,
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
    last_tool_msg_at = 0.0

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
                        # 推送工具调用进度,但限频
                        now = time.monotonic()
                        if now - last_tool_msg_at >= 0.5:
                            last_tool_msg_at = now
                            tip = _format_tool_use(block)
                            if tip:
                                await feishu_client.send_text(open_id, tip)
                    elif isinstance(block, ToolResultBlock):
                        # 工具结果不推送(避免刷屏),只在出错时提示
                        if block.is_error:
                            await feishu_client.send_text(
                                open_id, "⚠️ 工具调用出错,Claude 会自己重试或换方法"
                            )

            elif isinstance(msg, ResultMessage):
                # 一轮结束,发送累积的文本回复
                final_text = "".join(text_buffer).strip()
                if final_text:
                    await feishu_client.send_text(open_id, final_text)

                # 显示成本/状态(可选)
                if msg.is_error:
                    await feishu_client.send_text(
                        open_id,
                        f"❌ 任务结束(异常):{msg.subtype}",
                    )
                elif msg.total_cost_usd:
                    logger.info(
                        "turn done: tokens=%s cost=$%.4f",
                        msg.usage,
                        msg.total_cost_usd,
                    )
                break  # 一轮结束,退出 receive 循环

            elif isinstance(msg, SystemMessage):
                pass  # 不展示系统消息
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("receive_response failed")
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
    return f"🔧 {name}"


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

        options = _build_options(open_id, project, project_root)
        client = ClaudeSDKClient(options=options)
        await client.connect()
        pooled = _PooledClient(client=client, project=project)
        _pool[key] = pooled
        return pooled


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
