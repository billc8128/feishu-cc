import asyncio
import importlib
import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

import httpx

os.environ.setdefault("ANTHROPIC_AUTH_TOKEN", "test-token")
os.environ.setdefault("BROWSER_SERVICE_BASE_URL", "https://browser.example.com")
os.environ.setdefault("BROWSER_SERVICE_TOKEN", "browser-token")
os.environ.setdefault("FEISHU_APP_ID", "test-app-id")
os.environ.setdefault("FEISHU_APP_SECRET", "test-app-secret")
os.environ.setdefault("DATA_DIR", "/tmp/feishu-cc-test-data")


def _install_sdk_stub() -> None:
    fake_sdk = types.ModuleType("claude_agent_sdk")

    def tool(name, description, schema):
        def decorator(fn):
            fn._tool_name = name
            fn._tool_description = description
            fn._tool_schema = schema
            return fn

        return decorator

    def create_sdk_mcp_server(name, version, tools):
        return {
            "name": name,
            "version": version,
            "tools": {tool_fn._tool_name: tool_fn for tool_fn in tools},
        }

    fake_sdk.tool = tool
    fake_sdk.create_sdk_mcp_server = create_sdk_mcp_server
    sys.modules["claude_agent_sdk"] = fake_sdk


def _install_apscheduler_stub() -> None:
    fake_apscheduler = types.ModuleType("apscheduler")
    fake_jobstores = types.ModuleType("apscheduler.jobstores")
    fake_sqlalchemy = types.ModuleType("apscheduler.jobstores.sqlalchemy")
    fake_schedulers = types.ModuleType("apscheduler.schedulers")
    fake_asyncio = types.ModuleType("apscheduler.schedulers.asyncio")
    fake_triggers = types.ModuleType("apscheduler.triggers")
    fake_cron = types.ModuleType("apscheduler.triggers.cron")

    class _SQLAlchemyJobStore:
        def __init__(self, *args, **kwargs) -> None:
            pass

    class _AsyncIOScheduler:
        def __init__(self, *args, **kwargs) -> None:
            pass

    class _CronTrigger:
        @classmethod
        def from_crontab(cls, expr: str):
            return expr

    fake_sqlalchemy.SQLAlchemyJobStore = _SQLAlchemyJobStore
    fake_asyncio.AsyncIOScheduler = _AsyncIOScheduler
    fake_cron.CronTrigger = _CronTrigger

    sys.modules.setdefault("apscheduler", fake_apscheduler)
    sys.modules.setdefault("apscheduler.jobstores", fake_jobstores)
    sys.modules.setdefault("apscheduler.jobstores.sqlalchemy", fake_sqlalchemy)
    sys.modules.setdefault("apscheduler.schedulers", fake_schedulers)
    sys.modules.setdefault("apscheduler.schedulers.asyncio", fake_asyncio)
    sys.modules.setdefault("apscheduler.triggers", fake_triggers)
    sys.modules.setdefault("apscheduler.triggers.cron", fake_cron)


_install_sdk_stub()
_install_apscheduler_stub()
settings = importlib.import_module("config").settings
settings.browser_service_base_url = "https://browser.example.com"
settings.browser_service_token = "browser-token"
browser_client_module = importlib.import_module("agent.browser_client")
browser_tools = importlib.import_module("agent.tools_browser")


class _FakeAsyncClient:
    def __init__(self, request_handler, *args, **kwargs) -> None:
        self._request_handler = request_handler

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def request(self, method, url, *, json=None, headers=None):
        return await self._request_handler(method, url, json=json, headers=headers)


class BrowserServiceClientTests(unittest.TestCase):
    def test_client_translates_takeover_pause_to_typed_error(self) -> None:
        async def run_test() -> None:
            client = browser_client_module.BrowserServiceClient()

            async def request_handler(method, url, *, json=None, headers=None):
                return httpx.Response(
                    409,
                    json={"detail": "BROWSER_PAUSED_FOR_TAKEOVER"},
                    request=httpx.Request(method, url, json=json, headers=headers),
                )

            with patch.object(
                browser_client_module,
                "httpx",
                wraps=browser_client_module.httpx,
            ) as httpx_module:
                httpx_module.AsyncClient = lambda *args, **kwargs: _FakeAsyncClient(  # type: ignore[assignment]
                    request_handler, *args, **kwargs
                )
                with self.assertRaises(browser_client_module.BrowserPausedForTakeoverError):
                    await client.navigate("ou_123", "https://example.com")

        asyncio.run(run_test())

    def test_client_returns_none_for_allow_404(self) -> None:
        async def run_test() -> None:
            client = browser_client_module.BrowserServiceClient()

            async def request_handler(method, url, *, json=None, headers=None):
                return httpx.Response(
                    404,
                    json={"detail": "session not found"},
                    request=httpx.Request(method, url, json=json, headers=headers),
                )

            with patch.object(
                browser_client_module,
                "httpx",
                wraps=browser_client_module.httpx,
            ) as httpx_module:
                httpx_module.AsyncClient = lambda *args, **kwargs: _FakeAsyncClient(  # type: ignore[assignment]
                    request_handler, *args, **kwargs
                )
                result = await client.get_session("ou_missing")

            self.assertIsNone(result)

        asyncio.run(run_test())

    def test_client_takeover_and_resume_use_expected_endpoints(self) -> None:
        async def run_test() -> None:
            client = browser_client_module.BrowserServiceClient()
            requests = []

            async def request_handler(method, url, *, json=None, headers=None):
                requests.append((method, url, json, headers))
                return httpx.Response(
                    200,
                    json={"state": "active"},
                    request=httpx.Request(method, url, json=json, headers=headers),
                )

            with patch.object(
                browser_client_module,
                "httpx",
                wraps=browser_client_module.httpx,
            ) as httpx_module:
                httpx_module.AsyncClient = lambda *args, **kwargs: _FakeAsyncClient(  # type: ignore[assignment]
                    request_handler, *args, **kwargs
                )
                await client.takeover("ou_123")
                await client.resume("ou_123")

            self.assertEqual(
                [request[:2] for request in requests],
                [
                    ("POST", "https://browser.example.com/v1/sessions/ou_123/takeover"),
                    ("POST", "https://browser.example.com/v1/sessions/ou_123/resume"),
                ],
            )

        asyncio.run(run_test())

    def test_client_get_active_session_uses_expected_endpoint(self) -> None:
        async def run_test() -> None:
            client = browser_client_module.BrowserServiceClient()
            requests = []

            async def request_handler(method, url, *, json=None, headers=None):
                requests.append((method, url, json, headers))
                return httpx.Response(
                    200,
                    json={"open_id": "ou_active", "state": "active"},
                    request=httpx.Request(method, url, json=json, headers=headers),
                )

            with patch.object(
                browser_client_module,
                "httpx",
                wraps=browser_client_module.httpx,
            ) as httpx_module:
                httpx_module.AsyncClient = lambda *args, **kwargs: _FakeAsyncClient(  # type: ignore[assignment]
                    request_handler, *args, **kwargs
                )
                result = await client.get_active_session()

            self.assertEqual(result, {"open_id": "ou_active", "state": "active"})
            self.assertEqual(
                [request[:2] for request in requests],
                [("GET", "https://browser.example.com/v1/sessions/active")],
            )

        asyncio.run(run_test())


class BrowserToolsTests(unittest.TestCase):
    def test_browser_open_tool_description_mentions_view_or_takeover_link(self) -> None:
        server = browser_tools.build_browser_mcp("ou_123")

        self.assertIn("viewer", server["tools"]["browser_open"]._tool_description.lower())
        self.assertNotIn("spectator URL", server["tools"]["browser_open"]._tool_description)

    def test_browser_open_requests_permission_and_returns_viewer_url(self) -> None:
        async def run_test() -> None:
            server = browser_tools.build_browser_mcp("ou_123")
            browser_open = server["tools"]["browser_open"]

            with patch(
                "agent.tools_browser.browser_client.get_session",
                new=AsyncMock(return_value=None),
            ), patch(
                "agent.tools_browser.browser_client.ensure_session",
                new=AsyncMock(return_value={"state": "ready", "viewer_url": "https://viewer/session-1"}),
            ), patch(
                "agent.tools_browser.browser_approval.start_request",
                return_value=(types.SimpleNamespace(request_id="req-1", card_message_id=None, reason="需要登录 Reddit", trust_note=""), True),
            ), patch(
                "agent.tools_browser.browser_approval.wait_for_decision",
                new=AsyncMock(return_value=True),
            ), patch.object(
                browser_tools.feishu_client,
                "send_browser_approval_card",
                new=AsyncMock(return_value="om_card"),
            ) as send_card, patch.object(
                browser_tools.feishu_client,
                "send_text",
                new=AsyncMock(),
            ) as send_text:
                result = await browser_open({"reason": "需要登录 Reddit"})

            self.assertFalse(result.get("is_error", False))
            self.assertIn("https://viewer/session-1", result["content"][0]["text"])
            send_card.assert_awaited_once_with("ou_123", reason="需要登录 Reddit", request_id="req-1")
            self.assertIn("旁观/接管链接", send_text.await_args_list[0].args[1])

        asyncio.run(run_test())

    def test_browser_open_returns_error_when_user_denies(self) -> None:
        async def run_test() -> None:
            server = browser_tools.build_browser_mcp("ou_123")
            browser_open = server["tools"]["browser_open"]

            with patch(
                "agent.tools_browser.browser_client.get_session",
                new=AsyncMock(return_value=None),
            ), patch(
                "agent.tools_browser.browser_approval.start_request",
                return_value=(types.SimpleNamespace(request_id="req-1", card_message_id=None, reason="需要登录 Reddit", trust_note=""), True),
            ), patch(
                "agent.tools_browser.browser_approval.wait_for_decision",
                new=AsyncMock(return_value=False),
            ), patch.object(
                browser_tools.feishu_client,
                "send_browser_approval_card",
                new=AsyncMock(return_value="om_card"),
            ), patch.object(
                browser_tools.feishu_client,
                "update_browser_approval_card",
                new=AsyncMock(return_value=True),
            ), patch.object(
                browser_tools.feishu_client,
                "send_text",
                new=AsyncMock(),
            ):
                result = await browser_open({"reason": "需要登录 Reddit"})

            self.assertTrue(result["is_error"])
            self.assertIn("denied", result["content"][0]["text"].lower())

        asyncio.run(run_test())

    def test_browser_open_marks_card_expired_when_approval_times_out(self) -> None:
        async def run_test() -> None:
            server = browser_tools.build_browser_mcp("ou_123")
            browser_open = server["tools"]["browser_open"]
            request = types.SimpleNamespace(
                request_id="req-timeout",
                card_message_id=None,
                reason="需要登录 Reddit",
                trust_note="",
            )

            with patch(
                "agent.tools_browser.browser_client.get_session",
                new=AsyncMock(return_value=None),
            ), patch(
                "agent.tools_browser.browser_approval.start_request",
                return_value=(request, True),
            ), patch(
                "agent.tools_browser.browser_approval.wait_for_decision",
                new=AsyncMock(side_effect=browser_tools.browser_approval.ApprovalTimeoutError("timed out")),
            ), patch.object(
                browser_tools.feishu_client,
                "send_browser_approval_card",
                new=AsyncMock(return_value="om_card_timeout"),
            ), patch.object(
                browser_tools.feishu_client,
                "update_browser_approval_card",
                new=AsyncMock(return_value=True),
            ) as update_card, patch.object(
                browser_tools.feishu_client,
                "send_text",
                new=AsyncMock(),
            ):
                result = await browser_open({"reason": "需要登录 Reddit"})

            self.assertTrue(result["is_error"])
            self.assertIn("timed out", result["content"][0]["text"].lower())
            update_card.assert_awaited_once_with(
                "om_card_timeout",
                state="expired",
                reason="需要登录 Reddit",
                trust_note=None,
            )

        asyncio.run(run_test())

    def test_scheduled_browser_open_skips_approval_when_task_is_trusted(self) -> None:
        async def run_test() -> None:
            server = browser_tools.build_browser_mcp("ou_123")
            browser_open = server["tools"]["browser_open"]
            scheduled_context = types.SimpleNamespace(source="scheduler", task_id="task-123")

            with patch(
                "agent.tools_browser.browser_client.get_session",
                new=AsyncMock(return_value=None),
            ), patch(
                "agent.tools_browser.run_context.get_current_task_context",
                return_value=scheduled_context,
            ), patch(
                "agent.tools_browser.scheduler_store.is_browser_trusted",
                return_value=True,
            ) as is_trusted, patch(
                "agent.tools_browser.browser_client.ensure_session",
                new=AsyncMock(return_value={"state": "ready", "viewer_url": "https://viewer/session-1"}),
            ) as ensure_session, patch(
                "agent.tools_browser.browser_approval.start_request",
                return_value=(object(), True),
            ) as start_request, patch(
                "agent.tools_browser.browser_approval.wait_for_decision",
                new=AsyncMock(return_value=True),
            ) as wait_for_decision, patch.object(
                browser_tools.feishu_client,
                "send_browser_approval_card",
                new=AsyncMock(return_value="om_card"),
            ) as send_card, patch.object(
                browser_tools.feishu_client,
                "send_text",
                new=AsyncMock(),
            ):
                result = await browser_open({"reason": "需要登录 Reddit"})

            self.assertFalse(result.get("is_error", False))
            self.assertIn("https://viewer/session-1", result["content"][0]["text"])
            is_trusted.assert_called_once_with("task-123", "ou_123")
            ensure_session.assert_awaited_once_with("ou_123")
            start_request.assert_not_called()
            wait_for_decision.assert_not_awaited()
            send_card.assert_not_awaited()

        asyncio.run(run_test())

    def test_scheduled_browser_open_persists_trust_after_first_approval(self) -> None:
        async def run_test() -> None:
            server = browser_tools.build_browser_mcp("ou_123")
            browser_open = server["tools"]["browser_open"]
            scheduled_context = types.SimpleNamespace(source="scheduler", task_id="task-123")

            with patch(
                "agent.tools_browser.browser_client.get_session",
                new=AsyncMock(return_value=None),
            ), patch(
                "agent.tools_browser.run_context.get_current_task_context",
                return_value=scheduled_context,
            ), patch(
                "agent.tools_browser.scheduler_store.is_browser_trusted",
                return_value=False,
            ) as is_trusted, patch(
                "agent.tools_browser.scheduler_store.approve_browser_trust",
            ) as approve_trust, patch(
                "agent.tools_browser.browser_client.ensure_session",
                new=AsyncMock(return_value={"state": "ready", "viewer_url": "https://viewer/session-1"}),
            ), patch(
                "agent.tools_browser.browser_approval.start_request",
                return_value=(types.SimpleNamespace(request_id="req-1", card_message_id=None, reason="需要登录 Reddit", trust_note="允许后，此定时任务后续将自动使用浏览器，不再重复询问。"), True),
            ), patch(
                "agent.tools_browser.browser_approval.wait_for_decision",
                new=AsyncMock(return_value=True),
            ), patch.object(
                browser_tools.feishu_client,
                "send_browser_approval_card",
                new=AsyncMock(return_value="om_card"),
            ) as send_card, patch.object(
                browser_tools.feishu_client,
                "send_text",
                new=AsyncMock(),
            ):
                result = await browser_open({"reason": "需要登录 Reddit"})

            self.assertFalse(result.get("is_error", False))
            is_trusted.assert_called_once_with("task-123", "ou_123")
            approve_trust.assert_called_once_with("task-123", "ou_123")
            send_card.assert_awaited_once_with(
                "ou_123",
                reason="需要登录 Reddit",
                request_id="req-1",
                trust_note="允许后，此定时任务后续将自动使用浏览器，不再重复询问。",
            )

        asyncio.run(run_test())

    def test_scheduled_browser_open_does_not_persist_trust_when_request_was_not_created_here(self) -> None:
        async def run_test() -> None:
            server = browser_tools.build_browser_mcp("ou_123")
            browser_open = server["tools"]["browser_open"]
            scheduled_context = types.SimpleNamespace(source="scheduler", task_id="task-123")

            with patch(
                "agent.tools_browser.browser_client.get_session",
                new=AsyncMock(return_value=None),
            ), patch(
                "agent.tools_browser.run_context.get_current_task_context",
                return_value=scheduled_context,
            ), patch(
                "agent.tools_browser.scheduler_store.is_browser_trusted",
                return_value=False,
            ), patch(
                "agent.tools_browser.scheduler_store.approve_browser_trust",
            ) as approve_trust, patch(
                "agent.tools_browser.browser_client.ensure_session",
                new=AsyncMock(return_value={"state": "ready", "viewer_url": "https://viewer/session-1"}),
            ), patch(
                "agent.tools_browser.browser_approval.start_request",
                return_value=(types.SimpleNamespace(request_id="req-1", card_message_id="om_existing", reason="需要登录 Reddit", trust_note="允许后，此定时任务后续将自动使用浏览器，不再重复询问。"), False),
            ), patch(
                "agent.tools_browser.browser_approval.wait_for_decision",
                new=AsyncMock(return_value=True),
            ), patch.object(
                browser_tools.feishu_client,
                "send_browser_approval_card",
                new=AsyncMock(return_value="om_card"),
            ) as send_card, patch.object(
                browser_tools.feishu_client,
                "send_text",
                new=AsyncMock(),
            ):
                result = await browser_open({"reason": "需要登录 Reddit"})

            self.assertFalse(result.get("is_error", False))
            approve_trust.assert_not_called()
            send_card.assert_not_awaited()

        asyncio.run(run_test())

    def test_chat_browser_open_still_requests_approval_even_if_scheduler_lookup_would_be_true(self) -> None:
        async def run_test() -> None:
            server = browser_tools.build_browser_mcp("ou_123")
            browser_open = server["tools"]["browser_open"]
            chat_context = types.SimpleNamespace(source="chat", task_id="task-123")

            with patch(
                "agent.tools_browser.browser_client.get_session",
                new=AsyncMock(return_value=None),
            ), patch(
                "agent.tools_browser.run_context.get_current_task_context",
                return_value=chat_context,
            ), patch(
                "agent.tools_browser.scheduler_store.is_browser_trusted",
                return_value=True,
            ) as is_trusted, patch(
                "agent.tools_browser.browser_client.ensure_session",
                new=AsyncMock(return_value={"state": "ready", "viewer_url": "https://viewer/session-1"}),
            ) as ensure_session, patch(
                "agent.tools_browser.browser_approval.start_request",
                return_value=(types.SimpleNamespace(request_id="req-1", card_message_id=None, reason="需要登录 Reddit", trust_note=""), True),
            ) as start_request, patch(
                "agent.tools_browser.browser_approval.wait_for_decision",
                new=AsyncMock(return_value=True),
            ), patch.object(
                browser_tools.feishu_client,
                "send_browser_approval_card",
                new=AsyncMock(return_value="om_card"),
            ) as send_card, patch.object(
                browser_tools.feishu_client,
                "send_text",
                new=AsyncMock(),
            ):
                result = await browser_open({"reason": "需要登录 Reddit"})

            self.assertFalse(result.get("is_error", False))
            is_trusted.assert_not_called()
            start_request.assert_called_once()
            ensure_session.assert_awaited_once_with("ou_123")
            send_card.assert_awaited_once_with("ou_123", reason="需要登录 Reddit", request_id="req-1")

        asyncio.run(run_test())

    def test_scheduled_trust_lookup_failure_does_not_fail_browser_open(self) -> None:
        async def run_test() -> None:
            server = browser_tools.build_browser_mcp("ou_123")
            browser_open = server["tools"]["browser_open"]
            scheduled_context = types.SimpleNamespace(source="scheduler", task_id="task-123")

            with patch(
                "agent.tools_browser.browser_client.get_session",
                new=AsyncMock(return_value=None),
            ), patch(
                "agent.tools_browser.run_context.get_current_task_context",
                return_value=scheduled_context,
            ), patch(
                "agent.tools_browser.scheduler_store.is_browser_trusted",
                side_effect=RuntimeError("lookup failed"),
            ) as is_trusted, patch(
                "agent.tools_browser.browser_client.ensure_session",
                new=AsyncMock(return_value={"state": "ready", "viewer_url": "https://viewer/session-1"}),
            ) as ensure_session, patch(
                "agent.tools_browser.browser_approval.start_request",
                return_value=(types.SimpleNamespace(request_id="req-1", card_message_id=None, reason="需要登录 Reddit", trust_note="允许后，此定时任务后续将自动使用浏览器，不再重复询问。"), True),
            ) as start_request, patch(
                "agent.tools_browser.browser_approval.wait_for_decision",
                new=AsyncMock(return_value=True),
            ), patch.object(
                browser_tools.feishu_client,
                "send_browser_approval_card",
                new=AsyncMock(return_value="om_card"),
            ) as send_card, patch.object(
                browser_tools.feishu_client,
                "send_text",
                new=AsyncMock(),
            ):
                result = await browser_open({"reason": "需要登录 Reddit"})

            self.assertFalse(result.get("is_error", False))
            is_trusted.assert_called_once_with("task-123", "ou_123")
            start_request.assert_called_once()
            ensure_session.assert_awaited_once_with("ou_123")
            send_card.assert_awaited_once_with(
                "ou_123",
                reason="需要登录 Reddit",
                request_id="req-1",
                trust_note="允许后，此定时任务后续将自动使用浏览器，不再重复询问。",
            )

        asyncio.run(run_test())

    def test_scheduled_trust_persistence_failure_does_not_fail_browser_open(self) -> None:
        async def run_test() -> None:
            server = browser_tools.build_browser_mcp("ou_123")
            browser_open = server["tools"]["browser_open"]
            scheduled_context = types.SimpleNamespace(source="scheduler", task_id="task-123")

            with patch(
                "agent.tools_browser.browser_client.get_session",
                new=AsyncMock(return_value=None),
            ), patch(
                "agent.tools_browser.run_context.get_current_task_context",
                return_value=scheduled_context,
            ), patch(
                "agent.tools_browser.scheduler_store.is_browser_trusted",
                return_value=False,
            ), patch(
                "agent.tools_browser.scheduler_store.approve_browser_trust",
                side_effect=RuntimeError("persist failed"),
            ) as approve_trust, patch(
                "agent.tools_browser.browser_client.ensure_session",
                new=AsyncMock(return_value={"state": "ready", "viewer_url": "https://viewer/session-1"}),
            ) as ensure_session, patch(
                "agent.tools_browser.browser_approval.start_request",
                return_value=(types.SimpleNamespace(request_id="req-1", card_message_id=None, reason="需要登录 Reddit", trust_note="允许后，此定时任务后续将自动使用浏览器，不再重复询问。"), True),
            ), patch(
                "agent.tools_browser.browser_approval.wait_for_decision",
                new=AsyncMock(return_value=True),
            ), patch.object(
                browser_tools.feishu_client,
                "send_browser_approval_card",
                new=AsyncMock(return_value="om_card"),
            ), patch.object(
                browser_tools.feishu_client,
                "send_text",
                new=AsyncMock(),
            ):
                result = await browser_open({"reason": "需要登录 Reddit"})

            self.assertFalse(result.get("is_error", False))
            approve_trust.assert_called_once_with("task-123", "ou_123")
            ensure_session.assert_awaited_once_with("ou_123")
            self.assertIn("https://viewer/session-1", result["content"][0]["text"])

        asyncio.run(run_test())

    def test_browser_navigate_calls_browser_client(self) -> None:
        async def run_test() -> None:
            server = browser_tools.build_browser_mcp("ou_123")
            browser_navigate = server["tools"]["browser_navigate"]

            with patch(
                "agent.tools_browser.browser_client.navigate",
                new=AsyncMock(return_value={"state": "active", "url": "https://example.com"}),
            ) as navigate:
                result = await browser_navigate({"url": "https://example.com"})

            navigate.assert_awaited_once_with("ou_123", "https://example.com")
            self.assertIn("example.com", result["content"][0]["text"])

        asyncio.run(run_test())

    def test_browser_tools_report_takeover_pause_with_resume_message(self) -> None:
        async def run_test() -> None:
            server = browser_tools.build_browser_mcp("ou_123")
            tool_names = [
                ("browser_navigate", {"url": "https://example.com"}),
                ("browser_click", {"selector": "#cta"}),
                ("browser_type", {"selector": "#q", "text": "hello", "clear": True}),
                ("browser_wait", {"selector": "#ready", "timeout_ms": 500}),
                ("browser_snapshot", {}),
            ]

            for tool_name, args in tool_names:
                with self.subTest(tool=tool_name):
                    tool_fn = server["tools"][tool_name]
                    patch_target = f"agent.tools_browser.browser_client.{tool_name.removeprefix('browser_')}"
                    with patch(
                        patch_target,
                        new=AsyncMock(side_effect=browser_client_module.BrowserPausedForTakeoverError()),
                    ):
                        result = await tool_fn(args)

                    self.assertTrue(result["is_error"])
                    self.assertIn("Resume Agent", result["content"][0]["text"])
                    self.assertIn("浏览器已交给你", result["content"][0]["text"])

        asyncio.run(run_test())

    def test_browser_snapshot_formats_browser_state(self) -> None:
        async def run_test() -> None:
            server = browser_tools.build_browser_mcp("ou_123")
            browser_snapshot = server["tools"]["browser_snapshot"]

            with patch(
                "agent.tools_browser.browser_client.snapshot",
                new=AsyncMock(
                    return_value={
                        "state": "active",
                        "snapshot": {
                            "title": "Example Domain",
                            "url": "https://example.com",
                            "text": "Example Domain text",
                        },
                    }
                ),
            ):
                result = await browser_snapshot({})

            self.assertIn("Example Domain", result["content"][0]["text"])
            self.assertIn("https://example.com", result["content"][0]["text"])

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
