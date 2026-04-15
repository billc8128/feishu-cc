import asyncio
import importlib
import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("ANTHROPIC_AUTH_TOKEN", "test-token")
os.environ.setdefault("FEISHU_APP_ID", "test-app-id")
os.environ.setdefault("FEISHU_APP_SECRET", "test-app-secret")


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


_install_sdk_stub()
browser_tools = importlib.import_module("agent.tools_browser")


class BrowserToolsTests(unittest.TestCase):
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
                return_value=(object(), True),
            ), patch(
                "agent.tools_browser.browser_approval.wait_for_decision",
                new=AsyncMock(return_value=True),
            ), patch.object(
                browser_tools.feishu_client,
                "send_text",
                new=AsyncMock(),
            ) as send_text:
                result = await browser_open({"reason": "需要登录 Reddit"})

            self.assertFalse(result.get("is_error", False))
            self.assertIn("https://viewer/session-1", result["content"][0]["text"])
            self.assertIn("/browser yes", send_text.await_args_list[0].args[1])
            self.assertIn("旁观/接管链接", send_text.await_args_list[1].args[1])

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
                return_value=(object(), True),
            ), patch(
                "agent.tools_browser.browser_approval.wait_for_decision",
                new=AsyncMock(return_value=False),
            ), patch.object(
                browser_tools.feishu_client,
                "send_text",
                new=AsyncMock(),
            ):
                result = await browser_open({"reason": "需要登录 Reddit"})

            self.assertTrue(result["is_error"])
            self.assertIn("denied", result["content"][0]["text"].lower())

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
                        new=AsyncMock(side_effect=RuntimeError("BROWSER_PAUSED_FOR_TAKEOVER")),
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
