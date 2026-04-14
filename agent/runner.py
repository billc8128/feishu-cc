"""Claude Agent SDK 集成核心。

职责:
  1. 按 (open_id, project) 维护 ClaudeSDKClient 池,带空闲超时清理
  2. 配置 GLM 后端(通过 env 注入到 SDK)
  3. 流式接收 SDK 消息,实时翻译成飞书消息发出去
  4. 处理 session_id 持久化(下次进来 resume)
  5. 提供 interrupt(open_id) 让 /stop 命令能终止当前任务
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

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
]


def _build_options(open_id: str, project: str, project_root: str) -> ClaudeAgentOptions:
    """每个 (user, project) 一份独立的 options。"""
    schedule_server = build_schedule_mcp(open_id)

    resume_id = project_state.get_session_id(open_id, project)

    return ClaudeAgentOptions(
        cwd=project_root,
        # 加载项目级 CLAUDE.md / skills / commands(对齐 Claude Code)
        setting_sources=["project"],
        # 飞书没法弹审批框,所以 bypass。安全由 hooks 兜底。
        permission_mode="bypassPermissions",
        allowed_tools=DEFAULT_ALLOWED_TOOLS,
        hooks=build_hooks(open_id),
        mcp_servers={"schedule": schedule_server},
        resume=resume_id,  # 首次为 None,SDK 会创建新 session
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

    # 先发一条"思考中"占位消息,后续追加内容到新消息(飞书不支持长时间编辑同一条)
    await feishu_client.send_text(open_id, f"🤔 [{project}] 思考中…")

    try:
        await client.query(text)
    except Exception as exc:
        logger.exception("client.query failed")
        await feishu_client.send_text(open_id, f"❌ 出错:{exc}")
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

                # 持久化 session_id
                if msg.session_id:
                    project_state.set_session_id(open_id, project, msg.session_id)

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
        await feishu_client.send_text(open_id, f"❌ 接收响应出错:{exc}")


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
