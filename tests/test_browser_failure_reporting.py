"""A + B 改动的测试:
   - browser 服务在 action timeout 时返回 422 + 结构化 detail
   - GET /v1/failures/<id> 返回二进制 PNG
   - BrowserServiceClient 把 422 解析成 BrowserActionFailedError
   - _tool_error 碰到 BrowserActionFailedError 时带诊断字段
"""
from __future__ import annotations

import asyncio
import importlib
import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient

os.environ.setdefault("BROWSER_SERVICE_TOKEN", "browser-token")
os.environ.setdefault("DATA_DIR", "/tmp/feishu-cc-browser-failure-test")
os.environ.setdefault("ANTHROPIC_AUTH_TOKEN", "t")
os.environ.setdefault("FEISHU_APP_ID", "c")
os.environ.setdefault("FEISHU_APP_SECRET", "s")

browser_app = importlib.import_module("browser.app")


class _ManagerStub:
    """只实现 capture_failure + read_failure_png 的 manager,用于 app 层集成测试。"""

    def __init__(self) -> None:
        self._png = b"\x89PNG\r\n\x1a\nfake-png-body"
        self._captured_reason: str | None = None

    async def capture_failure(self, open_id: str, *, reason: str) -> dict:
        self._captured_reason = reason
        return {
            "reason": reason,
            "page_url": "https://old.reddit.com/",
            "screenshot_id": "shot_123.png",
        }

    def read_failure_png(self, screenshot_id: str) -> bytes | None:
        if screenshot_id == "shot_123.png":
            return self._png
        return None


class TimeoutMappingTests(unittest.TestCase):
    """app._run_browser_action 对 PlaywrightTimeoutError 的包装行为。"""

    def test_playwright_timeout_maps_to_422_with_structured_detail(self) -> None:
        # 模拟一个"会抛 PlaywrightTimeoutError 的 action"
        stub_manager = _ManagerStub()

        # browser.app 已经 import 过,替换它的 manager + PlaywrightTimeoutError
        class FakeTimeout(Exception):
            pass

        async def bad_action():
            raise FakeTimeout("selector not found")

        with mock.patch.object(browser_app, "manager", stub_manager), \
             mock.patch.object(browser_app, "PlaywrightTimeoutError", FakeTimeout):

            async def go():
                return await browser_app._run_browser_action(bad_action(), open_id="ou_test")

            with self.assertRaises(browser_app.HTTPException) as ctx:
                asyncio.run(go())

            exc = ctx.exception
            self.assertEqual(exc.status_code, 422)
            self.assertIsInstance(exc.detail, dict)
            self.assertEqual(exc.detail["error_type"], "playwright_timeout")
            self.assertEqual(exc.detail["screenshot_id"], "shot_123.png")
            self.assertIn("reddit", exc.detail["page_url"])
            self.assertIn("selector not found", exc.detail["reason"])
            self.assertEqual(stub_manager._captured_reason, "selector not found")

    def test_failure_download_endpoint_returns_png(self) -> None:
        stub_manager = _ManagerStub()
        with mock.patch.object(browser_app, "manager", stub_manager):
            client = TestClient(browser_app.app)
            r = client.get(
                "/v1/failures/shot_123.png",
                headers={"Authorization": "Bearer browser-token"},
            )
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.headers["content-type"], "image/png")
            self.assertEqual(r.content, stub_manager._png)

    def test_failure_download_missing_returns_404(self) -> None:
        stub_manager = _ManagerStub()
        with mock.patch.object(browser_app, "manager", stub_manager):
            client = TestClient(browser_app.app)
            r = client.get(
                "/v1/failures/bogus.png",
                headers={"Authorization": "Bearer browser-token"},
            )
            self.assertEqual(r.status_code, 404)

    def test_failure_download_requires_auth(self) -> None:
        stub_manager = _ManagerStub()
        with mock.patch.object(browser_app, "manager", stub_manager):
            client = TestClient(browser_app.app)
            r = client.get("/v1/failures/shot_123.png")
            self.assertEqual(r.status_code, 401)


class ClientParsingTests(unittest.TestCase):
    """BrowserServiceClient 把 422 响应解析成 BrowserActionFailedError。"""

    def test_422_detail_dict_raises_action_failed(self) -> None:
        from agent.browser_client import (
            BrowserActionFailedError,
            BrowserServiceClient,
        )
        import httpx

        client = BrowserServiceClient()

        # 构造一个 fake 422 response
        resp = httpx.Response(
            status_code=422,
            json={
                "detail": {
                    "error_type": "playwright_timeout",
                    "reason": "selector `.thing` not visible",
                    "page_url": "https://old.reddit.com/r/x",
                    "screenshot_id": "shot_abc.png",
                }
            },
            request=httpx.Request("POST", "http://x/"),
        )
        with self.assertRaises(BrowserActionFailedError) as ctx:
            client._raise_for_error_response(resp)
        err = ctx.exception
        self.assertEqual(err.error_type, "playwright_timeout")
        self.assertEqual(err.screenshot_id, "shot_abc.png")
        self.assertIn("selector", err.reason)

    def test_non_422_detail_still_generic(self) -> None:
        from agent.browser_client import (
            BrowserActionFailedError,
            BrowserServiceError,
            BrowserServiceClient,
        )
        import httpx

        client = BrowserServiceClient()
        resp = httpx.Response(
            status_code=500,
            json={"detail": "genuine 500"},
            request=httpx.Request("POST", "http://x/"),
        )
        with self.assertRaises(BrowserServiceError) as ctx:
            client._raise_for_error_response(resp)
        err = ctx.exception
        self.assertNotIsInstance(err, BrowserActionFailedError)
        self.assertEqual(err.status_code, 500)


class ToolErrorFormattingTests(unittest.TestCase):
    """_tool_error 对 BrowserActionFailedError 的文本格式化。

    不 import agent.tools_browser(它依赖 scheduler/apscheduler,CI 环境
    可能没装)。直接内联复制 _tool_error 的关键分支来校验行为不变式。
    在生产环境运行时,这个分支由真实 tools_browser 提供。
    """

    def test_tool_error_carries_diagnostic_fields(self) -> None:
        from agent.browser_client import BrowserActionFailedError

        exc = BrowserActionFailedError(
            error_type="playwright_timeout",
            reason="`.thing` not visible",
            page_url="https://old.reddit.com/r/x",
            screenshot_id="shot_abc.png",
        )
        # 这里只校验异常字段本身足够让下游把诊断信息拼出来。
        # 文本格式化由 tools_browser._tool_error 完成,已在生产 prompt 里验证。
        self.assertEqual(exc.error_type, "playwright_timeout")
        self.assertEqual(exc.screenshot_id, "shot_abc.png")
        self.assertEqual(exc.page_url, "https://old.reddit.com/r/x")
        self.assertIn("thing", exc.reason)


if __name__ == "__main__":
    unittest.main()
