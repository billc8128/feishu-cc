import asyncio
import importlib
import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient

os.environ.setdefault("BROWSER_SERVICE_TOKEN", "browser-token")
os.environ.setdefault("DATA_DIR", "/tmp/feishu-cc-browser-test-data")

browser_app = importlib.import_module("browser.app")
viewer_page = importlib.import_module("browser.viewer_page")
browser_service = importlib.import_module("browser.service")


class _FakeManager:
    def __init__(self) -> None:
        self._session = {
            "open_id": "ou_test",
            "state": "active",
            "controller": "agent",
            "paused_reason": "",
            "last_control_change_at": 0.0,
            "viewer_url": "https://browser.example.com/view/viewer-ou_test",
            "viewer_token": "viewer-ou_test",
        }
        self.raise_on_takeover = False
        self.raise_on_resume = False
        self.viewer_takeover_token = None
        self.viewer_resume_token = None
        self.viewer_lookup_token = None
        self.viewer_interact_token = None
        self.viewer_activity_tokens = []
        self.raise_on_action = None

    async def get_session(self, open_id: str):
        if open_id == self._session["open_id"]:
            return dict(self._session)
        return None

    async def get_session_by_viewer_token(self, viewer_token: str):
        self.viewer_lookup_token = viewer_token
        if viewer_token == self._session["viewer_token"]:
            return dict(self._session)
        return None

    async def can_viewer_interact(self, viewer_token: str):
        self.viewer_interact_token = viewer_token
        if viewer_token != self._session["viewer_token"]:
            return False
        return self._session["controller"] == "human"

    async def validate_viewer_token(self, viewer_token: str):
        return viewer_token == self._session["viewer_token"]

    async def record_viewer_activity(self, viewer_token: str):
        self.viewer_activity_tokens.append(viewer_token)

    async def takeover(self, open_id: str):
        if self.raise_on_takeover:
            raise RuntimeError("no active browser session for this user")
        if open_id != self._session["open_id"]:
            raise RuntimeError("no active browser session for this user")
        self._session["controller"] = "human"
        self._session["paused_reason"] = "takeover"
        self._session["last_control_change_at"] = 123.0
        return dict(self._session)

    async def resume(self, open_id: str):
        if self.raise_on_resume:
            raise RuntimeError("no active browser session for this user")
        if open_id != self._session["open_id"]:
            raise RuntimeError("no active browser session for this user")
        self._session["controller"] = "agent"
        self._session["paused_reason"] = ""
        self._session["last_control_change_at"] = 456.0
        return dict(self._session)

    async def takeover_by_viewer_token(self, viewer_token: str):
        self.viewer_takeover_token = viewer_token
        if viewer_token != self._session["viewer_token"]:
            raise RuntimeError("viewer session not found")
        return await self.takeover(self._session["open_id"])

    async def resume_by_viewer_token(self, viewer_token: str):
        self.viewer_resume_token = viewer_token
        if viewer_token != self._session["viewer_token"]:
            raise RuntimeError("viewer session not found")
        return await self.resume(self._session["open_id"])

    async def navigate(self, open_id: str, url: str):
        if self.raise_on_action:
            raise RuntimeError(self.raise_on_action)
        return {"url": url}

    async def click(self, open_id: str, selector: str):
        if self.raise_on_action:
            raise RuntimeError(self.raise_on_action)
        return {"clicked": selector}

    async def type(self, open_id: str, selector: str, text: str, *, clear: bool):
        if self.raise_on_action:
            raise RuntimeError(self.raise_on_action)
        return {"typed": text}

    async def wait(self, open_id: str, *, selector: str = "", text: str = "", timeout_ms: int = 10_000):
        if self.raise_on_action:
            raise RuntimeError(self.raise_on_action)
        return {"waited": selector}

    async def snapshot(self, open_id: str):
        if self.raise_on_action:
            raise RuntimeError(self.raise_on_action)
        return {"open_id": open_id}


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
        self.assertIn('id="takeover-button" type="button"', response.text)
        self.assertIn('id="resume-button" type="button" disabled', response.text)
        self.assertEqual(browser_app.manager.viewer_lookup_token, "viewer-ou_test")

    def test_view_starts_interactive_when_session_is_human_controlled(self) -> None:
        browser_app.manager._session["controller"] = "human"
        browser_app.manager._session["paused_reason"] = "takeover"
        browser_app.manager._session["last_control_change_at"] = 123.0

        response = self.client.get("/view/viewer-ou_test", follow_redirects=False)

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "Human takeover active. Agent control is paused.",
            response.text,
        )
        self.assertIn('id="takeover-button" type="button" disabled', response.text)
        self.assertIn('id="resume-button" type="button"', response.text)
        self.assertIn(
            'src="/novnc/vnc_lite.html?path=ws/viewer-ou_test&amp;autoconnect=1&amp;resize=scale"',
            response.text,
        )
        self.assertNotIn(
            'src="/novnc/vnc_lite.html?path=ws/viewer-ou_test&amp;autoconnect=1&amp;view_only=1&amp;resize=scale"',
            response.text,
        )

    def test_takeover_switches_session_controller_to_human(self) -> None:
        response = self.client.post("/v1/sessions/ou_test/takeover", headers=self.headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["controller"], "human")
        self.assertEqual(browser_app.manager._session["controller"], "human")

    def test_resume_switches_session_controller_back_to_agent(self) -> None:
        browser_app.manager._session["controller"] = "human"
        browser_app.manager._session["paused_reason"] = "takeover"

        response = self.client.post("/v1/sessions/ou_test/resume", headers=self.headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["controller"], "agent")
        self.assertEqual(browser_app.manager._session["controller"], "agent")

    def test_takeover_runtime_error_returns_conflict_instead_of_500(self) -> None:
        browser_app.manager.raise_on_takeover = True

        response = self.client.post("/v1/sessions/ou_test/takeover", headers=self.headers)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "no active browser session for this user")

    def test_resume_runtime_error_returns_conflict_instead_of_500(self) -> None:
        browser_app.manager.raise_on_resume = True

        response = self.client.post("/v1/sessions/ou_test/resume", headers=self.headers)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "no active browser session for this user")

    def test_paused_browser_actions_return_conflict_instead_of_500(self) -> None:
        browser_app.manager.raise_on_action = browser_service.TAKEOVER_PAUSED_ERROR

        responses = [
            self.client.post("/v1/sessions/ou_test/navigate", headers=self.headers, json={"url": "https://example.com"}),
            self.client.post("/v1/sessions/ou_test/click", headers=self.headers, json={"selector": "#cta"}),
            self.client.post(
                "/v1/sessions/ou_test/type",
                headers=self.headers,
                json={"selector": "#q", "text": "hello", "clear": True},
            ),
            self.client.post(
                "/v1/sessions/ou_test/wait",
                headers=self.headers,
                json={"selector": "#q", "text": "", "timeout_ms": 1000},
            ),
            self.client.post("/v1/sessions/ou_test/snapshot", headers=self.headers),
        ]

        for response in responses:
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["detail"], browser_service.TAKEOVER_PAUSED_ERROR)

    def test_viewer_takeover_endpoint_uses_viewer_token(self) -> None:
        response = self.client.post("/view/viewer-ou_test/takeover")
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["controller"], "human")
        self.assertEqual(
            set(payload.keys()),
            {"state", "controller", "paused_reason", "last_control_change_at"},
        )
        self.assertNotIn("open_id", payload)
        self.assertNotIn("viewer_token", payload)
        self.assertEqual(browser_app.manager._session["controller"], "human")
        self.assertEqual(browser_app.manager.viewer_takeover_token, "viewer-ou_test")

    def test_viewer_resume_endpoint_returns_conflict_for_inactive_token(self) -> None:
        browser_app.manager.raise_on_resume = True

        response = self.client.post("/view/viewer-ou_test/resume")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "no active browser session for this user")
        self.assertEqual(browser_app.manager.viewer_resume_token, "viewer-ou_test")

    def test_viewer_takeover_endpoint_returns_conflict_for_unknown_token(self) -> None:
        response = self.client.post("/view/viewer-missing/takeover")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "viewer session not found")
        self.assertEqual(browser_app.manager.viewer_takeover_token, "viewer-missing")

    def test_viewer_resume_endpoint_returns_conflict_for_unknown_token(self) -> None:
        response = self.client.post("/view/viewer-missing/resume")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "viewer session not found")
        self.assertEqual(browser_app.manager.viewer_resume_token, "viewer-missing")

    def test_view_unknown_viewer_token_returns_not_found(self) -> None:
        response = self.client.get("/view/viewer-missing")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "viewer session not found")
        self.assertEqual(browser_app.manager.viewer_lookup_token, "viewer-missing")

    def test_viewer_resume_endpoint_omits_internal_session_fields(self) -> None:
        browser_app.manager._session["controller"] = "human"
        browser_app.manager._session["paused_reason"] = "takeover"

        response = self.client.post("/view/viewer-ou_test/resume")
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["controller"], "agent")
        self.assertEqual(
            set(payload.keys()),
            {"state", "controller", "paused_reason", "last_control_change_at"},
        )
        self.assertNotIn("open_id", payload)
        self.assertNotIn("viewer_token", payload)

    def test_viewer_controls_include_network_failure_fallback(self) -> None:
        response = self.client.get("/view/viewer-ou_test")

        self.assertIn("try {", response.text)
        self.assertIn("catch (error)", response.text)
        self.assertIn("Connection issue. Please try again.", response.text)
        self.assertIn("takeoverButton.disabled = true;", response.text)
        self.assertIn("resumeButton.disabled = false;", response.text)
        self.assertIn("takeoverButton.disabled = false;", response.text)
        self.assertIn("resumeButton.disabled = true;", response.text)
        self.assertIn("function enterTerminalState(statusText)", response.text)
        self.assertIn('frameNode.src = "about:blank";', response.text)
        self.assertIn("Viewer session ended. Reload to reconnect.", response.text)
        self.assertIn("enterTerminalState", response.text)

    def test_render_viewer_page_escapes_quote_and_newline_tokens_safely(self) -> None:
        html = viewer_page.render_viewer_page(
            viewer_token='viewer"\nline',
            controller="agent",
            status_text="Viewer ready. Use Take Over to pause the agent or Resume Agent to hand control back.",
            interactive=False,
        )

        self.assertIn('"viewerToken": "viewer\\"\\nline"', html)
        self.assertIn("/view/viewer%22%0Aline/takeover", html)
        self.assertIn("/view/viewer%22%0Aline/resume", html)
        self.assertIn("path=ws/viewer%22%0Aline&amp;autoconnect=1&amp;view_only=1&amp;resize=scale", html)

    def test_render_viewer_page_reflects_initial_human_control_state(self) -> None:
        html = viewer_page.render_viewer_page(
            viewer_token="viewer-ou_test",
            controller="human",
            status_text="Human takeover active. Agent control is paused.",
            interactive=True,
        )

        self.assertIn("Human takeover active. Agent control is paused.", html)
        self.assertIn('"controller": "human"', html)
        self.assertIn('"interactive": true', html)
        self.assertIn('id="takeover-button" type="button" disabled', html)
        self.assertIn('id="resume-button" type="button"', html)
        self.assertIn(
            'src="/novnc/vnc_lite.html?path=ws/viewer-ou_test&amp;autoconnect=1&amp;resize=scale"',
            html,
        )

    def test_websocket_forwards_protocol_messages_when_agent_controls_session(self) -> None:
        async def run_test() -> None:
            class _FakeWebSocket:
                def __init__(self) -> None:
                    self.closed_code = None
                    self.accepted = False
                    self.messages = [
                        {"type": "websocket.receive", "bytes": b"\x03\x01\x00\x00\x00\x00\x05\xa0\x03\xc0"},
                        {"type": "websocket.disconnect"},
                    ]

                async def accept(self) -> None:
                    self.accepted = True

                async def receive(self):
                    return self.messages.pop(0)

                async def send_bytes(self, data: bytes) -> None:
                    return None

                async def close(self, code=None) -> None:
                    self.closed_code = code

            class _FakeReader:
                async def read(self, n: int) -> bytes:
                    await asyncio.Future()

            class _FakeWriter:
                def __init__(self) -> None:
                    self.writes = []

                def write(self, data: bytes) -> None:
                    self.writes.append(data)

                async def drain(self) -> None:
                    return None

                def close(self) -> None:
                    return None

                async def wait_closed(self) -> None:
                    return None

            websocket = _FakeWebSocket()
            writer = _FakeWriter()

            with mock.patch.object(browser_app, "manager", browser_app.manager), mock.patch.object(
                browser_app.manager, "validate_viewer_token", new=mock.AsyncMock(return_value=True)
            ), mock.patch.object(
                browser_app.manager, "can_viewer_interact", new=mock.AsyncMock(return_value=False)
            ), mock.patch.object(
                browser_app.asyncio, "open_connection", new=mock.AsyncMock(return_value=(_FakeReader(), writer))
            ):
                await browser_app.vnc_websocket(websocket, "viewer-ou_test")

            self.assertTrue(websocket.accepted)
            self.assertEqual(writer.writes, [b"\x03\x01\x00\x00\x00\x00\x05\xa0\x03\xc0"])

        asyncio.run(run_test())

    def test_websocket_blocks_pointer_input_when_agent_controls_session(self) -> None:
        async def run_test() -> None:
            class _FakeWebSocket:
                def __init__(self) -> None:
                    self.closed_code = None
                    self.accepted = False
                    self.messages = [
                        {"type": "websocket.receive", "bytes": b"\x05\x01\x00\x10\x00\x20"},
                        {"type": "websocket.disconnect"},
                    ]

                async def accept(self) -> None:
                    self.accepted = True

                async def receive(self):
                    return self.messages.pop(0)

                async def send_bytes(self, data: bytes) -> None:
                    return None

                async def close(self, code=None) -> None:
                    self.closed_code = code

            class _FakeReader:
                async def read(self, n: int) -> bytes:
                    await asyncio.Future()

            class _FakeWriter:
                def __init__(self) -> None:
                    self.writes = []

                def write(self, data: bytes) -> None:
                    self.writes.append(data)

                async def drain(self) -> None:
                    return None

                def close(self) -> None:
                    return None

                async def wait_closed(self) -> None:
                    return None

            websocket = _FakeWebSocket()
            writer = _FakeWriter()

            with mock.patch.object(browser_app, "manager", browser_app.manager), mock.patch.object(
                browser_app.manager, "validate_viewer_token", new=mock.AsyncMock(return_value=True)
            ), mock.patch.object(
                browser_app.manager, "can_viewer_interact", new=mock.AsyncMock(return_value=False)
            ), mock.patch.object(
                browser_app.asyncio, "open_connection", new=mock.AsyncMock(return_value=(_FakeReader(), writer))
            ):
                await browser_app.vnc_websocket(websocket, "viewer-ou_test")

            self.assertTrue(websocket.accepted)
            self.assertEqual(writer.writes, [])

        asyncio.run(run_test())

    def test_websocket_records_viewer_activity_during_human_control(self) -> None:
        async def run_test() -> None:
            class _FakeWebSocket:
                def __init__(self) -> None:
                    self.closed_code = None
                    self.accepted = False
                    self.messages = [
                        {"type": "websocket.receive", "bytes": b"\x03\x01\x00\x00\x00\x00\x05\xa0\x03\xc0"},
                        {"type": "websocket.disconnect"},
                    ]

                async def accept(self) -> None:
                    self.accepted = True

                async def receive(self):
                    return self.messages.pop(0)

                async def send_bytes(self, data: bytes) -> None:
                    return None

                async def close(self, code=None) -> None:
                    self.closed_code = code

            class _FakeReader:
                async def read(self, n: int) -> bytes:
                    await asyncio.Future()

            class _FakeWriter:
                def write(self, data: bytes) -> None:
                    return None

                async def drain(self) -> None:
                    return None

                def close(self) -> None:
                    return None

                async def wait_closed(self) -> None:
                    return None

            websocket = _FakeWebSocket()

            with mock.patch.object(browser_app, "manager", browser_app.manager), mock.patch.object(
                browser_app.manager, "validate_viewer_token", new=mock.AsyncMock(return_value=True)
            ), mock.patch.object(
                browser_app.manager, "can_viewer_interact", new=mock.AsyncMock(return_value=True)
            ), mock.patch.object(
                browser_app.asyncio,
                "open_connection",
                new=mock.AsyncMock(return_value=(_FakeReader(), _FakeWriter())),
            ):
                await browser_app.vnc_websocket(websocket, "viewer-ou_test")

            self.assertEqual(browser_app.manager.viewer_activity_tokens, ["viewer-ou_test"])

        asyncio.run(run_test())
