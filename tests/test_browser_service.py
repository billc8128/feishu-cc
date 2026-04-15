import asyncio
import importlib
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("BROWSER_SERVICE_TOKEN", "browser-token")
os.environ.setdefault("DATA_DIR", "/tmp/feishu-cc-browser-test-data")


browser_service = importlib.import_module("browser.service")


class _FakeDriver:
    def __init__(self) -> None:
        self.started = []
        self.stopped = []

    async def start(self, *, open_id: str, profile_dir: Path, public_base_url: str):
        self.started.append((open_id, str(profile_dir), public_base_url))
        return {
            "viewer_token": f"viewer-{open_id}",
            "viewer_url": f"{public_base_url}/view/{open_id}",
        }

    async def stop(self, open_id: str) -> None:
        self.stopped.append(open_id)


class BrowserServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.driver = _FakeDriver()
        self.manager = browser_service.BrowserSessionManager(
            data_dir=Path(self._tmp.name),
            driver=self.driver,
            idle_timeout_seconds=300,
            max_session_ttl_seconds=1800,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_ensure_session_starts_immediately_when_idle(self) -> None:
        async def run_test() -> None:
            session = await self.manager.ensure_session(
                "ou_a", public_base_url="https://browser.example.com"
            )

            self.assertEqual(session["state"], "active")
            self.assertEqual(session["viewer_url"], "https://browser.example.com/view/ou_a")
            self.assertEqual(self.driver.started[0][0], "ou_a")

        asyncio.run(run_test())

    def test_second_user_is_queued_while_first_is_active(self) -> None:
        async def run_test() -> None:
            await self.manager.ensure_session("ou_a", public_base_url="https://browser.example.com")
            session = await self.manager.ensure_session("ou_b", public_base_url="https://browser.example.com")

            self.assertEqual(session["state"], "queued")
            self.assertEqual(session["queue_position"], 1)

        asyncio.run(run_test())

    def test_same_user_reuses_existing_session(self) -> None:
        async def run_test() -> None:
            first = await self.manager.ensure_session("ou_a", public_base_url="https://browser.example.com")
            second = await self.manager.ensure_session("ou_a", public_base_url="https://browser.example.com")

            self.assertEqual(first["viewer_url"], second["viewer_url"])
            self.assertEqual(len(self.driver.started), 1)

        asyncio.run(run_test())

    def test_close_promotes_next_queued_user(self) -> None:
        async def run_test() -> None:
            await self.manager.ensure_session("ou_a", public_base_url="https://browser.example.com")
            await self.manager.ensure_session("ou_b", public_base_url="https://browser.example.com")

            closed = await self.manager.close_session("ou_a", public_base_url="https://browser.example.com")
            next_session = await self.manager.get_session("ou_b")

            self.assertEqual(closed["state"], "closed")
            self.assertEqual(next_session["state"], "active")
            self.assertEqual(self.driver.stopped, ["ou_a"])
            self.assertEqual([item[0] for item in self.driver.started], ["ou_a", "ou_b"])

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
