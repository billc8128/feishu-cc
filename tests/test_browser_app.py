import importlib
import os
import unittest

from fastapi.testclient import TestClient

os.environ.setdefault("BROWSER_SERVICE_TOKEN", "browser-token")
os.environ.setdefault("DATA_DIR", "/tmp/feishu-cc-browser-test-data")

browser_app = importlib.import_module("browser.app")


class _FakeManager:
    def __init__(self) -> None:
        self.sessions = {
            "ou_test": {
                "open_id": "ou_test",
                "state": "active",
                "controller": "agent",
                "paused_reason": "",
                "last_control_change_at": 0.0,
                "viewer_url": "https://browser.example.com/view/viewer-ou_test",
                "viewer_token": "viewer-ou_test",
            }
        }
        self.raise_on_takeover = False
        self.raise_on_resume = False

    async def get_session(self, open_id: str):
        return self.sessions.get(open_id)

    async def takeover(self, open_id: str):
        if self.raise_on_takeover:
            raise RuntimeError("no active browser session for this user")
        session = self.sessions[open_id]
        session["controller"] = "human"
        session["paused_reason"] = "takeover"
        session["last_control_change_at"] = 123.0
        return dict(session)

    async def resume(self, open_id: str):
        if self.raise_on_resume:
            raise RuntimeError("no active browser session for this user")
        session = self.sessions[open_id]
        session["controller"] = "agent"
        session["paused_reason"] = ""
        session["last_control_change_at"] = 456.0
        return dict(session)


class BrowserAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self._original_manager = browser_app.manager
        browser_app.manager = _FakeManager()
        self.client = TestClient(browser_app.app)
        self.headers = {"Authorization": "Bearer browser-token"}

    def tearDown(self) -> None:
        browser_app.manager = self._original_manager

    def test_view_returns_wrapper_html_with_takeover_controls(self) -> None:
        response = self.client.get("/view/viewer-ou_test", follow_redirects=False)

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("Take Over", response.text)
        self.assertIn("Resume Agent", response.text)
        self.assertIn("/novnc/vnc_lite.html", response.text)
        self.assertIn("view_only=1", response.text)
        self.assertIn("/view/viewer-ou_test/takeover", response.text)
        self.assertIn("/view/viewer-ou_test/resume", response.text)

    def test_takeover_switches_session_controller_to_human(self) -> None:
        response = self.client.post("/v1/sessions/ou_test/takeover", headers=self.headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["controller"], "human")
        self.assertEqual(browser_app.manager.sessions["ou_test"]["controller"], "human")

    def test_resume_switches_session_controller_back_to_agent(self) -> None:
        browser_app.manager.sessions["ou_test"]["controller"] = "human"
        browser_app.manager.sessions["ou_test"]["paused_reason"] = "takeover"

        response = self.client.post("/v1/sessions/ou_test/resume", headers=self.headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["controller"], "agent")
        self.assertEqual(browser_app.manager.sessions["ou_test"]["controller"], "agent")

    def test_takeover_runtime_error_returns_conflict_instead_of_500(self) -> None:
        browser_app.manager.raise_on_takeover = True

        response = self.client.post("/v1/sessions/ou_test/takeover", headers=self.headers)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "no active browser session for this user")

    def test_resume_runtime_error_returns_conflict_instead_of_500(self) -> None:
        browser_app.manager.raise_on_resume = True

        response = self.client.post("/v1/sessions/ou_test/resume", headers=self.headers)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "no active browser session for this user")

    def test_viewer_takeover_endpoint_uses_viewer_token(self) -> None:
        response = self.client.post("/view/viewer-ou_test/takeover")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["controller"], "human")
        self.assertEqual(browser_app.manager.sessions["ou_test"]["controller"], "human")

    def test_viewer_resume_endpoint_returns_conflict_for_inactive_token(self) -> None:
        browser_app.manager.raise_on_resume = True

        response = self.client.post("/view/viewer-ou_test/resume")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "no active browser session for this user")
