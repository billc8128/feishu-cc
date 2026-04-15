import asyncio
import importlib
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

os.environ.setdefault("BROWSER_SERVICE_TOKEN", "browser-token")
os.environ.setdefault("DATA_DIR", "/tmp/feishu-cc-browser-test-data")


browser_service = importlib.import_module("browser.service")


class _FakeDriver:
    def __init__(self) -> None:
        self.started = []
        self.stopped = []
        self.navigated = []

    async def start(self, *, open_id: str, profile_dir: Path, public_base_url: str):
        self.started.append((open_id, str(profile_dir), public_base_url))
        return {
            "viewer_token": f"viewer-{open_id}",
            "viewer_url": f"{public_base_url}/view/{open_id}",
        }

    async def stop(self, open_id: str) -> None:
        self.stopped.append(open_id)

    async def navigate(self, open_id: str, url: str):
        self.navigated.append((open_id, url))
        return {"url": url}


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
            self.assertEqual(session["controller"], "agent")
            self.assertEqual(session["paused_reason"], "")
            self.assertEqual(session["last_control_change_at"], 0.0)
            self.assertEqual(session["viewer_url"], "https://browser.example.com/view/ou_a")
            self.assertEqual(self.driver.started[0][0], "ou_a")

        asyncio.run(run_test())

    def test_second_user_is_queued_while_first_is_active(self) -> None:
        async def run_test() -> None:
            await self.manager.ensure_session("ou_a", public_base_url="https://browser.example.com")
            session = await self.manager.ensure_session("ou_b", public_base_url="https://browser.example.com")
            queued_session = await self.manager.get_session("ou_b")

            self.assertEqual(session["state"], "queued")
            self.assertEqual(session["queue_position"], 1)
            self.assertEqual(session["controller"], "agent")
            self.assertEqual(session["paused_reason"], "")
            self.assertEqual(session["last_control_change_at"], 0.0)
            self.assertEqual(queued_session["controller"], "agent")
            self.assertEqual(queued_session["paused_reason"], "")
            self.assertEqual(queued_session["last_control_change_at"], 0.0)

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

    def test_close_promotes_queued_user_with_its_own_base_url(self) -> None:
        async def run_test() -> None:
            await self.manager.ensure_session("ou_a", public_base_url="https://browser-a.example.com")
            await self.manager.ensure_session("ou_b", public_base_url="https://browser-b.example.com")

            await self.manager.close_session("ou_a", public_base_url="https://browser-a.example.com")
            promoted = await self.manager.get_session("ou_b")

            self.assertEqual(promoted["state"], "active")
            self.assertEqual(promoted["viewer_url"], "https://browser-b.example.com/view/ou_b")
            self.assertEqual(self.driver.started[-1][2], "https://browser-b.example.com")

        asyncio.run(run_test())

    def test_takeover_switches_controller_to_human(self) -> None:
        async def run_test() -> None:
            await self.manager.ensure_session("ou_a", public_base_url="https://browser.example.com")

            session = await self.manager.takeover("ou_a")
            stored = await self.manager.get_session("ou_a")

            self.assertEqual(session["controller"], "human")
            self.assertEqual(session["paused_reason"], "takeover")
            self.assertEqual(stored["controller"], "human")
            self.assertEqual(stored["paused_reason"], "takeover")

        asyncio.run(run_test())

    def test_takeover_is_idempotent_when_already_human_controlled(self) -> None:
        async def run_test() -> None:
            clock = {"now": 100.0}

            def fake_monotonic() -> float:
                return clock["now"]

            manager = browser_service.BrowserSessionManager(
                data_dir=Path(self._tmp.name),
                driver=self.driver,
                idle_timeout_seconds=300,
                max_session_ttl_seconds=1800,
            )

            with mock.patch.object(browser_service.time, "monotonic", side_effect=fake_monotonic):
                await manager.ensure_session("ou_a", public_base_url="https://browser.example.com")
                first = await manager.takeover("ou_a")
                first_snapshot = {
                    "controller": manager._sessions["ou_a"].controller,
                    "paused_reason": manager._sessions["ou_a"].paused_reason,
                    "last_control_change_at": manager._sessions["ou_a"].last_control_change_at,
                    "last_used_at": manager._sessions["ou_a"].last_used_at,
                }

                clock["now"] = 101.0
                second = await manager.takeover("ou_a")
                second_snapshot = {
                    "controller": manager._sessions["ou_a"].controller,
                    "paused_reason": manager._sessions["ou_a"].paused_reason,
                    "last_control_change_at": manager._sessions["ou_a"].last_control_change_at,
                    "last_used_at": manager._sessions["ou_a"].last_used_at,
                }

            self.assertEqual(second, first)
            self.assertEqual(second_snapshot, first_snapshot)

        asyncio.run(run_test())

    def test_takeover_refreshes_activity_before_idle_expiry(self) -> None:
        async def run_test() -> None:
            clock = {"now": 100.0}

            def fake_monotonic() -> float:
                return clock["now"]

            manager = browser_service.BrowserSessionManager(
                data_dir=Path(self._tmp.name),
                driver=self.driver,
                idle_timeout_seconds=5,
                max_session_ttl_seconds=1800,
            )

            with mock.patch.object(browser_service.time, "monotonic", side_effect=fake_monotonic):
                await manager.ensure_session("ou_a", public_base_url="https://browser.example.com")
                initial_last_used_at = manager._sessions["ou_a"].last_used_at

                clock["now"] = 104.9
                await manager.takeover("ou_a")
                takeover_control_change_at = manager._sessions["ou_a"].last_control_change_at
                takeover_last_used_at = manager._sessions["ou_a"].last_used_at

                clock["now"] = 105.1
                session = await manager.ensure_session(
                    "ou_a", public_base_url="https://browser.example.com"
                )

            self.assertEqual(session["state"], "active")
            self.assertEqual(self.driver.stopped, [])
            self.assertEqual(len(self.driver.started), 1)
            self.assertGreater(takeover_last_used_at, initial_last_used_at)
            self.assertGreater(takeover_control_change_at, initial_last_used_at)

        asyncio.run(run_test())

    def test_takeover_expires_stale_session_before_transition(self) -> None:
        async def run_test() -> None:
            clock = {"now": 100.0}

            def fake_monotonic() -> float:
                return clock["now"]

            manager = browser_service.BrowserSessionManager(
                data_dir=Path(self._tmp.name),
                driver=self.driver,
                idle_timeout_seconds=5,
                max_session_ttl_seconds=1800,
            )

            with mock.patch.object(browser_service.time, "monotonic", side_effect=fake_monotonic):
                await manager.ensure_session("ou_a", public_base_url="https://browser.example.com")
                clock["now"] = 106.0

                with self.assertRaisesRegex(RuntimeError, "no active browser session for this user"):
                    await manager.takeover("ou_a")

            self.assertEqual(self.driver.stopped, ["ou_a"])
            self.assertIsNone(await manager.get_session("ou_a"))

        asyncio.run(run_test())

    def test_takeover_expires_stale_session_and_promotes_queued_follower(self) -> None:
        async def run_test() -> None:
            clock = {"now": 300.0}

            def fake_monotonic() -> float:
                return clock["now"]

            manager = browser_service.BrowserSessionManager(
                data_dir=Path(self._tmp.name),
                driver=self.driver,
                idle_timeout_seconds=5,
                max_session_ttl_seconds=1800,
            )

            with mock.patch.object(browser_service.time, "monotonic", side_effect=fake_monotonic):
                await manager.ensure_session("ou_a", public_base_url="https://browser.example.com")
                await manager.ensure_session("ou_b", public_base_url="https://browser.example.com")
                clock["now"] = 306.0

                with self.assertRaisesRegex(RuntimeError, "no active browser session for this user"):
                    await manager.takeover("ou_a")

            promoted = await manager.get_session("ou_b")
            self.assertEqual(self.driver.stopped, ["ou_a"])
            self.assertEqual(promoted["state"], "active")
            self.assertEqual(promoted["viewer_url"], "https://browser.example.com/view/ou_b")
            self.assertEqual([item[0] for item in self.driver.started], ["ou_a", "ou_b"])

        asyncio.run(run_test())

    def test_resume_switches_controller_back_to_agent(self) -> None:
        async def run_test() -> None:
            await self.manager.ensure_session("ou_a", public_base_url="https://browser.example.com")
            await self.manager.takeover("ou_a")

            session = await self.manager.resume("ou_a")
            stored = await self.manager.get_session("ou_a")

            self.assertEqual(session["controller"], "agent")
            self.assertEqual(session["paused_reason"], "")
            self.assertEqual(stored["controller"], "agent")
            self.assertEqual(stored["paused_reason"], "")

        asyncio.run(run_test())

    def test_resume_is_idempotent_when_already_agent_controlled(self) -> None:
        async def run_test() -> None:
            clock = {"now": 200.0}

            def fake_monotonic() -> float:
                return clock["now"]

            manager = browser_service.BrowserSessionManager(
                data_dir=Path(self._tmp.name),
                driver=self.driver,
                idle_timeout_seconds=300,
                max_session_ttl_seconds=1800,
            )

            with mock.patch.object(browser_service.time, "monotonic", side_effect=fake_monotonic):
                await manager.ensure_session("ou_a", public_base_url="https://browser.example.com")
                first = await manager.resume("ou_a")
                first_snapshot = {
                    "controller": manager._sessions["ou_a"].controller,
                    "paused_reason": manager._sessions["ou_a"].paused_reason,
                    "last_control_change_at": manager._sessions["ou_a"].last_control_change_at,
                    "last_used_at": manager._sessions["ou_a"].last_used_at,
                }

                clock["now"] = 201.0
                second = await manager.resume("ou_a")
                second_snapshot = {
                    "controller": manager._sessions["ou_a"].controller,
                    "paused_reason": manager._sessions["ou_a"].paused_reason,
                    "last_control_change_at": manager._sessions["ou_a"].last_control_change_at,
                    "last_used_at": manager._sessions["ou_a"].last_used_at,
                }

            self.assertEqual(second, first)
            self.assertEqual(second_snapshot, first_snapshot)

        asyncio.run(run_test())

    def test_resume_reenables_browser_actions_after_takeover(self) -> None:
        async def run_test() -> None:
            clock = {"now": 200.0}

            def fake_monotonic() -> float:
                return clock["now"]

            manager = browser_service.BrowserSessionManager(
                data_dir=Path(self._tmp.name),
                driver=self.driver,
                idle_timeout_seconds=5,
                max_session_ttl_seconds=1800,
            )

            with mock.patch.object(browser_service.time, "monotonic", side_effect=fake_monotonic):
                await manager.ensure_session("ou_a", public_base_url="https://browser.example.com")
                await manager.takeover("ou_a")
                takeover_last_used_at = manager._sessions["ou_a"].last_used_at
                takeover_control_change_at = manager._sessions["ou_a"].last_control_change_at

                clock["now"] = 204.5
                await manager.resume("ou_a")
                resumed_last_used_at = manager._sessions["ou_a"].last_used_at
                resumed_control_change_at = manager._sessions["ou_a"].last_control_change_at

                result = await manager.navigate("ou_a", "https://example.com")

            self.assertEqual(result["url"], "https://example.com")
            self.assertEqual(manager._sessions["ou_a"].controller, "agent")
            self.assertGreater(resumed_last_used_at, takeover_last_used_at)
            self.assertGreater(resumed_control_change_at, takeover_control_change_at)

        asyncio.run(run_test())

    def test_resume_expires_stale_session_before_transition(self) -> None:
        async def run_test() -> None:
            clock = {"now": 200.0}

            def fake_monotonic() -> float:
                return clock["now"]

            manager = browser_service.BrowserSessionManager(
                data_dir=Path(self._tmp.name),
                driver=self.driver,
                idle_timeout_seconds=5,
                max_session_ttl_seconds=1800,
            )

            with mock.patch.object(browser_service.time, "monotonic", side_effect=fake_monotonic):
                await manager.ensure_session("ou_a", public_base_url="https://browser.example.com")
                await manager.takeover("ou_a")
                clock["now"] = 206.0

                with self.assertRaisesRegex(RuntimeError, "no active browser session for this user"):
                    await manager.resume("ou_a")

            self.assertEqual(self.driver.stopped, ["ou_a"])
            self.assertIsNone(await manager.get_session("ou_a"))

        asyncio.run(run_test())

    def test_browser_actions_fail_while_human_controls_session(self) -> None:
        async def run_test() -> None:
            await self.manager.ensure_session("ou_a", public_base_url="https://browser.example.com")
            await self.manager.takeover("ou_a")

            with self.assertRaisesRegex(RuntimeError, "BROWSER_PAUSED_FOR_TAKEOVER"):
                await self.manager.navigate("ou_a", "https://example.com")

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
