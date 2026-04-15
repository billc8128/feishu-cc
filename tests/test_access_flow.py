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
from feishu.events import ParsedMessageEvent


class AccessFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._original_data_dir = settings.data_dir
        self._original_admin_ids = getattr(settings, "feishu_admin_open_ids", "")
        self._original_allowed_ids = settings.feishu_allowed_open_ids
        settings.data_dir = self._tmp.name
        settings.feishu_admin_open_ids = "ou_admin"
        settings.feishu_allowed_open_ids = ""

        auth_store = importlib.import_module("auth.store")
        auth_store._initialized = False

    def tearDown(self) -> None:
        settings.data_dir = self._original_data_dir
        settings.feishu_allowed_open_ids = self._original_allowed_ids
        if hasattr(settings, "feishu_admin_open_ids"):
            settings.feishu_admin_open_ids = self._original_admin_ids
        self._tmp.cleanup()

    def test_apply_creates_pending_request_and_notifies_admin(self) -> None:
        async def run_test() -> None:
            auth_store = importlib.import_module("auth.store")
            parsed = ParsedMessageEvent(
                event_id="evt-1",
                sender_open_id="ou_guest",
                chat_id="oc_1",
                chat_type="p2p",
                message_id="om_1",
                text="/apply",
                attachments=[],
            )

            with patch.object(app_module.feishu_client, "send_text", new=AsyncMock()) as send_text:
                await app_module._dispatch(parsed)

            self.assertEqual(auth_store.get_user("ou_guest").status, "pending")
            recipients = [call.args[0] for call in send_text.await_args_list]
            self.assertEqual(recipients, ["ou_guest", "ou_admin"])

        asyncio.run(run_test())

    def test_unapproved_user_is_prompted_to_apply_without_entering_agent(self) -> None:
        async def run_test() -> None:
            parsed = ParsedMessageEvent(
                event_id="evt-2",
                sender_open_id="ou_guest",
                chat_id="oc_1",
                chat_type="p2p",
                message_id="om_2",
                text="帮我写个脚本",
                attachments=[],
            )

            with patch.object(app_module.feishu_client, "send_text", new=AsyncMock()) as send_text, patch(
                "agent.runner.handle_user_message",
                new=AsyncMock(),
            ) as handle_user_message:
                await app_module._dispatch(parsed)

            handle_user_message.assert_not_awaited()
            self.assertIn("/apply", send_text.await_args.args[1])

        asyncio.run(run_test())

    def test_admin_can_approve_pending_user(self) -> None:
        async def run_test() -> None:
            auth_store = importlib.import_module("auth.store")
            auth_store.request_access("ou_guest")
            parsed = ParsedMessageEvent(
                event_id="evt-3",
                sender_open_id="ou_admin",
                chat_id="oc_1",
                chat_type="p2p",
                message_id="om_3",
                text="/approve ou_guest",
                attachments=[],
            )

            with patch.object(app_module.feishu_client, "send_text", new=AsyncMock()) as send_text, patch(
                "agent.runner.handle_user_message",
                new=AsyncMock(),
            ) as handle_user_message:
                await app_module._dispatch(parsed)

            handle_user_message.assert_not_awaited()
            self.assertEqual(auth_store.get_user("ou_guest").status, "approved")
            recipients = [call.args[0] for call in send_text.await_args_list]
            self.assertEqual(recipients, ["ou_guest", "ou_admin"])

        asyncio.run(run_test())

    def test_admin_can_reject_pending_user_with_reason(self) -> None:
        async def run_test() -> None:
            auth_store = importlib.import_module("auth.store")
            auth_store.request_access("ou_guest")
            parsed = ParsedMessageEvent(
                event_id="evt-4",
                sender_open_id="ou_admin",
                chat_id="oc_1",
                chat_type="p2p",
                message_id="om_4",
                text="/reject ou_guest 信息不完整",
                attachments=[],
            )

            with patch.object(app_module.feishu_client, "send_text", new=AsyncMock()) as send_text:
                await app_module._dispatch(parsed)

            rejected = auth_store.get_user("ou_guest")
            self.assertEqual(rejected.status, "rejected")
            self.assertEqual(rejected.review_note, "信息不完整")
            recipients = [call.args[0] for call in send_text.await_args_list]
            self.assertEqual(recipients, ["ou_guest", "ou_admin"])

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
