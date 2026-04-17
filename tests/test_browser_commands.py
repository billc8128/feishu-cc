import asyncio
import importlib
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

os.environ.setdefault("ANTHROPIC_AUTH_TOKEN", "test-token")
os.environ.setdefault("FEISHU_APP_ID", "test-app-id")
os.environ.setdefault("FEISHU_APP_SECRET", "test-app-secret")
os.environ.setdefault("DATA_DIR", "/tmp/feishu-cc-test-data")
os.environ.setdefault("BROWSER_SERVICE_BASE_URL", "https://browser.example.com")
os.environ.setdefault("BROWSER_SERVICE_TOKEN", "browser-token")


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
from feishu.events import ParsedCardActionEvent, ParsedMessageEvent


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
            request, _ = browser_approval.start_request("ou_user", reason="登录", timeout_seconds=60)
            request.card_message_id = "om_card_1"
            parsed = ParsedMessageEvent(
                event_id="evt-1",
                sender_open_id="ou_user",
                chat_id="oc_1",
                chat_type="p2p",
                message_id="om_1",
                text="/browser yes",
                attachments=[],
            )

            with patch.object(app_module.feishu_client, "send_text", new=AsyncMock()) as send_text, patch.object(
                app_module.feishu_client,
                "update_browser_approval_card",
                new=AsyncMock(return_value=True),
            ) as update_card:
                await app_module._dispatch(parsed)

            self.assertEqual(browser_approval.get_request_status("ou_user"), "approved")
            self.assertIn("已允许", send_text.await_args.args[1])
            update_card.assert_awaited_once_with(
                "om_card_1",
                state="approved",
                reason="登录",
                trust_note=None,
            )

        asyncio.run(run_test())

    def test_browser_approval_card_action_resolves_pending_request(self) -> None:
        async def run_test() -> None:
            request, _ = browser_approval.start_request("ou_user", reason="登录", timeout_seconds=60)
            parsed = ParsedCardActionEvent(
                event_id="evt-card-1",
                operator_open_id="ou_user",
                open_message_id="om_card_1",
                action_tag="button",
                action_value={"kind": "browser_approval", "decision": "yes", "request_id": request.request_id},
            )

            with patch.object(
                app_module.feishu_client,
                "update_browser_approval_card",
                new=AsyncMock(return_value=True),
            ) as update_card:
                response = await app_module._handle_card_action(parsed)

            self.assertEqual(browser_approval.get_request_status("ou_user"), "approved")
            self.assertEqual(response["toast"]["content"], "✅ 已允许 agent 使用浏览器。")
            update_card.assert_awaited_once_with(
                "om_card_1",
                state="approved",
                reason="登录",
                trust_note=None,
            )

        asyncio.run(run_test())

    def test_feishu_webhook_accepts_legacy_card_action_callback(self) -> None:
        request, _ = browser_approval.start_request("ou_user", reason="登录", timeout_seconds=60)
        original_token = settings.feishu_verification_token
        settings.feishu_verification_token = "verification-token"
        client = TestClient(app_module.app)
        try:
            with patch.object(
                app_module.feishu_client,
                "update_browser_approval_card",
                new=AsyncMock(return_value=True),
            ):
                response = client.post(
                    "/feishu/webhook",
                    json={
                        "open_id": "ou_user",
                        "open_message_id": "om_card_legacy",
                        "token": "verification-token",
                        "action": {
                            "tag": "button",
                            "value": {
                                "kind": "browser_approval",
                                "decision": "yes",
                                "request_id": request.request_id,
                            },
                        },
                    },
                )
        finally:
            settings.feishu_verification_token = original_token

        self.assertEqual(response.status_code, 200)
        self.assertEqual(browser_approval.get_request_status("ou_user"), "approved")
        self.assertEqual(response.json()["toast"]["content"], "✅ 已允许 agent 使用浏览器。")

    def test_browser_no_denies_pending_request(self) -> None:
        async def run_test() -> None:
            request, _ = browser_approval.start_request("ou_user", reason="登录", timeout_seconds=60)
            request.card_message_id = "om_card_2"
            parsed = ParsedMessageEvent(
                event_id="evt-2",
                sender_open_id="ou_user",
                chat_id="oc_1",
                chat_type="p2p",
                message_id="om_2",
                text="/browser no",
                attachments=[],
            )

            with patch.object(app_module.feishu_client, "send_text", new=AsyncMock()) as send_text, patch.object(
                app_module.feishu_client,
                "update_browser_approval_card",
                new=AsyncMock(return_value=True),
            ) as update_card:
                await app_module._dispatch(parsed)

            self.assertEqual(browser_approval.get_request_status("ou_user"), "denied")
            self.assertIn("已取消", send_text.await_args.args[1])
            update_card.assert_awaited_once_with(
                "om_card_2",
                state="denied",
                reason="登录",
                trust_note=None,
            )

        asyncio.run(run_test())

    def test_browser_approval_card_action_marks_stale_card_when_request_id_mismatches(self) -> None:
        async def run_test() -> None:
            request, _ = browser_approval.start_request("ou_user", reason="登录", timeout_seconds=60)
            parsed = ParsedCardActionEvent(
                event_id="evt-card-2",
                operator_open_id="ou_user",
                open_message_id="om_card_old",
                action_tag="button",
                action_value={
                    "kind": "browser_approval",
                    "decision": "yes",
                    "request_id": f"{request.request_id}-stale",
                },
            )

            with patch.object(
                app_module.feishu_client,
                "update_browser_approval_card",
                new=AsyncMock(return_value=True),
            ) as update_card:
                response = await app_module._handle_card_action(parsed)

            self.assertEqual(browser_approval.get_request_status("ou_user"), "pending")
            self.assertEqual(response["toast"]["content"], "ℹ️ 这张浏览器授权卡片已失效或已处理。")
            update_card.assert_awaited_once_with(
                "om_card_old",
                state="stale",
                reason=None,
                trust_note=None,
            )

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

    def test_browser_who_reports_active_browser_owner_name(self) -> None:
        async def run_test() -> None:
            parsed = ParsedMessageEvent(
                event_id="evt-6",
                sender_open_id="ou_user",
                chat_id="oc_1",
                chat_type="p2p",
                message_id="om_6",
                text="/browser who",
                attachments=[],
            )

            with patch.object(app_module.feishu_client, "send_text", new=AsyncMock()) as send_text, patch(
                "agent.browser_client.browser_client.get_active_session",
                new=AsyncMock(return_value={"open_id": "ou_active", "state": "active", "controller": "agent"}),
            ), patch.object(
                app_module.feishu_client,
                "get_user_display_name",
                new=AsyncMock(return_value="朱政怡"),
            ):
                await app_module._dispatch(parsed)

            message = send_text.await_args.args[1]
            self.assertIn("朱政怡", message)
            self.assertIn("ou_active", message)

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
