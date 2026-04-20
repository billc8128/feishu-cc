"""PR 0 冒烟工具。

目的:验证 Kimi K2.5 (通过 api.moonshot.ai/anthropic) 能完整走通 tool_use 协议:
  模型 → 发出 tool_use block → SDK 调度到这里 → 返回 tool_result → 模型继续生成 → 回到用户

通过条件:在飞书里发 /smoke hello world,收到机器人回复里含 "echo_back: hello world"。
通不过(模型不会调工具、SDK 报错、字段错位等)则 Kimi 的 Anthropic 兼容端点在工具调用上
有缺陷,需要切到 Anthropic→OpenAI 代理方案。

通过后这个文件保留作为未来诊断用,但在 runner 里不再挂载。
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from claude_agent_sdk import create_sdk_mcp_server, tool

logger = logging.getLogger(__name__)


def build_smoke_mcp():
    """构造一个最小 MCP server,仅含一个 echo 工具。"""

    @tool(
        "smoke_echo",
        (
            "Diagnostic tool that echoes back the text you give it, prefixed with 'echo_back: '. "
            "Use this ONLY when the user's message starts with /smoke — that's the signal you "
            "should call this tool with the rest of the message as the text argument, then "
            "reply to the user with the tool's output."
        ),
        {
            "text": str,
        },
    )
    async def smoke_echo(args: Dict[str, Any]) -> Dict[str, Any]:
        text = (args.get("text") or "").strip()
        logger.info("smoke_echo invoked with text=%r", text[:80])
        return {
            "content": [
                {"type": "text", "text": f"echo_back: {text}"},
            ]
        }

    return create_sdk_mcp_server(
        name="smoke",
        version="0.1.0",
        tools=[smoke_echo],
    )
