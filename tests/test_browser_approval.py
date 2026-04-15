import asyncio
import importlib
import os
import unittest

os.environ.setdefault("ANTHROPIC_AUTH_TOKEN", "test-token")
os.environ.setdefault("FEISHU_APP_ID", "test-app-id")
os.environ.setdefault("FEISHU_APP_SECRET", "test-app-secret")


browser_approval = importlib.import_module("agent.browser_approval")


class BrowserApprovalTests(unittest.TestCase):
    def setUp(self) -> None:
        browser_approval.reset_state()

    def tearDown(self) -> None:
        browser_approval.reset_state()

    def test_start_request_creates_pending_entry(self) -> None:
        request, created = browser_approval.start_request(
            "ou_123",
            reason="需要登录 Reddit",
            timeout_seconds=30,
        )

        self.assertTrue(created)
        self.assertEqual(request.open_id, "ou_123")
        self.assertEqual(request.reason, "需要登录 Reddit")
        self.assertEqual(browser_approval.get_request_status("ou_123"), "pending")

    def test_resolve_request_marks_approved(self) -> None:
        async def run_test() -> None:
            browser_approval.start_request("ou_123", reason="登录", timeout_seconds=30)
            waiter = asyncio.create_task(browser_approval.wait_for_decision("ou_123"))

            resolved = browser_approval.resolve_request("ou_123", approved=True)

            self.assertTrue(resolved)
            self.assertTrue(await waiter)
            self.assertEqual(browser_approval.get_request_status("ou_123"), "approved")

        asyncio.run(run_test())

    def test_resolve_request_marks_denied(self) -> None:
        async def run_test() -> None:
            browser_approval.start_request("ou_123", reason="登录", timeout_seconds=30)
            waiter = asyncio.create_task(browser_approval.wait_for_decision("ou_123"))

            resolved = browser_approval.resolve_request("ou_123", approved=False)

            self.assertTrue(resolved)
            self.assertFalse(await waiter)
            self.assertEqual(browser_approval.get_request_status("ou_123"), "denied")

        asyncio.run(run_test())

    def test_wait_for_decision_times_out_and_expires_request(self) -> None:
        async def run_test() -> None:
            browser_approval.start_request("ou_123", reason="登录", timeout_seconds=0.01)

            with self.assertRaises(browser_approval.ApprovalTimeoutError):
                await browser_approval.wait_for_decision("ou_123")

            self.assertEqual(browser_approval.get_request_status("ou_123"), "expired")

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
