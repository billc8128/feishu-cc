import asyncio
import importlib
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

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
settings = importlib.import_module("config").settings
app_module = importlib.import_module("app")
browser_approval = importlib.import_module("agent.browser_approval")
auth_store = importlib.import_module("auth.store")
from feishu.events import ParsedMessageEvent


class BrowserCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._original_data_dir = settings.data_dir
        self._original_admin_ids = getattr(settings, "feishu_admin_open_ids", "")
        settings.data_dir = self._tmp.name
        settings.feishu_admin_open_ids = ""
        auth_store._initialized = False
        auth_store.approve_user("ou_user", "ou_admin")
        browser_approval.reset_state()

    def tearDown(self) -> None:
        settings.data_dir = self._original_data_dir
        settings.feishu_admin_open_ids = self._original_admin_ids
        browser_approval.reset_state()
        self._tmp.cleanup()

    def test_browser_yes_resolves_pending_request(self) -> None:
        async def run_test() -> None:
            browser_approval.start_request("ou_user", reason="登录", timeout_seconds=60)
            parsed = ParsedMessageEvent(
                event_id="evt-1",
                sender_open_id="ou_user",
                chat_id="oc_1",
                chat_type="p2p",
                message_id="om_1",
                text="/browser yes",
                attachments=[],
            )

            with patch.object(app_module.feishu_client, "send_text", new=AsyncMock()) as send_text:
                await app_module._dispatch(parsed)

            self.assertEqual(browser_approval.get_request_status("ou_user"), "approved")
            self.assertIn("已允许", send_text.await_args.args[1])

        asyncio.run(run_test())

    def test_browser_no_denies_pending_request(self) -> None:
        async def run_test() -> None:
            browser_approval.start_request("ou_user", reason="登录", timeout_seconds=60)
            parsed = ParsedMessageEvent(
                event_id="evt-2",
                sender_open_id="ou_user",
                chat_id="oc_1",
                chat_type="p2p",
                message_id="om_2",
                text="/browser no",
                attachments=[],
            )

            with patch.object(app_module.feishu_client, "send_text", new=AsyncMock()) as send_text:
                await app_module._dispatch(parsed)

            self.assertEqual(browser_approval.get_request_status("ou_user"), "denied")
            self.assertIn("已取消", send_text.await_args.args[1])

        asyncio.run(run_test())

    def test_browser_status_reports_no_active_session(self) -> None:
        async def run_test() -> None:
            parsed = ParsedMessageEvent(
                event_id="evt-3",
                sender_open_id="ou_user",
                chat_id="oc_1",
                chat_type="p2p",
                message_id="om_3",
                text="/browser status",
                attachments=[],
            )

            with patch.object(app_module.feishu_client, "send_text", new=AsyncMock()) as send_text, patch(
                "agent.browser_client.browser_client.get_session",
                new=AsyncMock(return_value=None),
            ):
                await app_module._dispatch(parsed)

            self.assertIn("当前没有浏览器会话", send_text.await_args.args[1])

        asyncio.run(run_test())

    def test_browser_status_reports_controller_state(self) -> None:
        async def run_test() -> None:
            parsed = ParsedMessageEvent(
                event_id="evt-4",
                sender_open_id="ou_user",
                chat_id="oc_1",
                chat_type="p2p",
                message_id="om_4",
                text="/browser status",
                attachments=[],
            )

            with patch.object(app_module.feishu_client, "send_text", new=AsyncMock()) as send_text, patch(
                "agent.browser_client.browser_client.get_session",
                new=AsyncMock(
                    return_value={
                        "state": "active",
                        "controller": "human",
                        "viewer_url": "https://viewer.example.com/session-1",
                    }
                ),
            ):
                await app_module._dispatch(parsed)

            message = send_text.await_args.args[1]
            self.assertIn("active", message.lower())
            self.assertIn("human", message.lower())
            self.assertIn("旁观/接管链接", message)
            self.assertIn("https://viewer.example.com/session-1", message)

        asyncio.run(run_test())

    def test_browser_close_closes_active_session(self) -> None:
        async def run_test() -> None:
            parsed = ParsedMessageEvent(
                event_id="evt-5",
                sender_open_id="ou_user",
                chat_id="oc_1",
                chat_type="p2p",
                message_id="om_5",
                text="/browser close",
                attachments=[],
            )

            with patch.object(app_module.feishu_client, "send_text", new=AsyncMock()) as send_text, patch(
                "agent.browser_client.browser_client.close_session",
                new=AsyncMock(return_value={"state": "closed"}),
            ):
                await app_module._dispatch(parsed)

            self.assertIn("已关闭", send_text.await_args.args[1])

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
