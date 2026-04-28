import asyncio
import importlib
import os
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("ANTHROPIC_AUTH_TOKEN", "test-token")
os.environ.setdefault("FEISHU_APP_ID", "test-app-id")
os.environ.setdefault("FEISHU_APP_SECRET", "test-app-secret")
os.environ.setdefault("DATA_DIR", "/tmp/feishu-cc-test-data")


def _install_test_stubs() -> None:
    fake_sdk = types.ModuleType("claude_agent_sdk")

    class _Dummy:
        def __init__(self, *args, **kwargs) -> None:
            pass

    fake_sdk.AssistantMessage = _Dummy
    fake_sdk.ClaudeAgentOptions = _Dummy
    fake_sdk.ClaudeSDKClient = _Dummy
    fake_sdk.ResultMessage = _Dummy
    fake_sdk.SystemMessage = _Dummy
    fake_sdk.TextBlock = _Dummy
    fake_sdk.ThinkingBlock = _Dummy
    fake_sdk.ToolResultBlock = _Dummy
    fake_sdk.ToolUseBlock = _Dummy
    fake_sdk.create_sdk_mcp_server = lambda *args, **kwargs: {}
    fake_sdk.tool = lambda *args, **kwargs: (lambda fn: fn)

    fake_hooks = types.ModuleType("agent.hooks")
    fake_hooks.build_hooks = lambda open_id: {}

    fake_schedule = types.ModuleType("agent.tools_schedule")
    fake_schedule.build_schedule_mcp = lambda open_id: {}

    fake_deliver = types.ModuleType("agent.tools_deliver")
    fake_deliver.build_deliver_mcp = lambda open_id: {}

    sys.modules.setdefault("claude_agent_sdk", fake_sdk)
    sys.modules.setdefault("agent.hooks", fake_hooks)
    sys.modules.setdefault("agent.tools_schedule", fake_schedule)
    sys.modules.setdefault("agent.tools_deliver", fake_deliver)


_install_test_stubs()
runner = importlib.import_module("agent.runner")


class _FakeAssistantMessage:
    def __init__(self, content) -> None:
        self.content = content


class _FakeTextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeToolUseBlock:
    def __init__(self, name: str, input=None) -> None:
        self.name = name
        self.input = input or {}


class _FakeToolResultBlock:
    def __init__(self, is_error: bool) -> None:
        self.is_error = is_error


class _FakeResultMessage:
    def __init__(
        self,
        *,
        session_id: str = "sess_1",
        is_error: bool = False,
        subtype: str = "",
        total_cost_usd: float = 0.0,
        usage=None,
    ) -> None:
        self.session_id = session_id
        self.is_error = is_error
        self.subtype = subtype
        self.total_cost_usd = total_cost_usd
        self.usage = usage or {}


class _FakeClient:
    def __init__(self, messages) -> None:
        self._messages = messages
        self.queries = []

    async def query(self, text: str) -> None:
        self.queries.append(text)

    async def receive_response(self):
        for msg in self._messages:
            yield msg


class RunnerProgressTests(unittest.TestCase):
    def test_run_query_aggregates_tool_updates_into_single_card(self) -> None:
        async def run_test() -> None:
            fake_client = _FakeClient(
                [
                    _FakeAssistantMessage(
                        [_FakeToolUseBlock("Bash", {"command": "echo hi"})]
                    ),
                    _FakeAssistantMessage(
                        [
                            _FakeToolUseBlock("mcp__browser__browser_navigate", {}),
                            _FakeToolUseBlock("mcp__browser__browser_click", {}),
                        ]
                    ),
                    _FakeAssistantMessage([_FakeTextBlock("最终结果")]),
                    _FakeResultMessage(total_cost_usd=0.12),
                ]
            )
            pooled = SimpleNamespace(client=fake_client)

            with patch.object(runner, "AssistantMessage", _FakeAssistantMessage), patch.object(
                runner, "TextBlock", _FakeTextBlock
            ), patch.object(runner, "ToolUseBlock", _FakeToolUseBlock), patch.object(
                runner, "ToolResultBlock", _FakeToolResultBlock
            ), patch.object(
                runner, "ResultMessage", _FakeResultMessage
            ), patch.object(
                runner.project_state, "set_active_session_id", new=Mock()
            ), patch.object(
                runner.feishu_client,
                "send_markdown",
                new=AsyncMock(side_effect=["om_run_card", "om_final"]),
            ) as send_markdown, patch.object(
                runner.feishu_client,
                "update_markdown",
                new=AsyncMock(return_value=True),
            ) as update_markdown, patch.object(
                runner.feishu_client,
                "send_text",
                new=AsyncMock(),
            ) as send_text, patch.object(
                runner.time,
                "monotonic",
                side_effect=[0.0, 0.2, 2.0],
            ):
                await runner._run_query("ou_123", "scratch", pooled, "帮我做点事")

            self.assertEqual(fake_client.queries, ["帮我做点事"])
            self.assertEqual(send_markdown.await_count, 2)
            first_call = send_markdown.await_args_list[0]
            self.assertEqual(first_call.args[0], "ou_123")
            self.assertIn("**状态**：正在执行工具", first_call.args[1])
            self.assertEqual(first_call.kwargs["title"], "任务运行中")

            self.assertGreaterEqual(update_markdown.await_count, 2)
            progress_update = update_markdown.await_args_list[0]
            self.assertEqual(progress_update.args[0], "om_run_card")
            self.assertIn("浏览器 2", progress_update.args[1])
            self.assertEqual(progress_update.kwargs["title"], "任务运行中")

            final_update = update_markdown.await_args_list[-1]
            self.assertEqual(final_update.kwargs["title"], "任务完成")
            self.assertIn("结果已在下方消息中给出。", final_update.args[1])

            send_text.assert_not_awaited()

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
