import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from browser.driver import PlaywrightBrowserDriver


class _FakeProcess:
    def __init__(self) -> None:
        self.returncode = 0


class BrowserDriverTests(unittest.TestCase):
    def test_start_removes_stale_chromium_singleton_files(self) -> None:
        async def run_test() -> None:
            driver = PlaywrightBrowserDriver()

            with tempfile.TemporaryDirectory() as tmp:
                profile_dir = Path(tmp) / "profile"
                profile_dir.mkdir(parents=True, exist_ok=True)
                stale_paths = [
                    profile_dir / "SingletonLock",
                    profile_dir / "SingletonSocket",
                    profile_dir / "SingletonCookie",
                ]
                for path in stale_paths:
                    path.write_text("stale")

                with patch(
                    "browser.driver.asyncio.create_subprocess_exec",
                    new=AsyncMock(side_effect=[_FakeProcess(), _FakeProcess(), _FakeProcess(), _FakeProcess()]),
                ), patch.object(
                    driver,
                    "_wait_for_x11",
                    new=AsyncMock(),
                ), patch.object(
                    driver,
                    "_wait_for_port",
                    new=AsyncMock(),
                ), patch.object(
                    driver,
                    "_connect_playwright",
                    new=AsyncMock(return_value=(object(), object(), object(), object())),
                ):
                    await driver.start(
                        open_id="ou_test",
                        profile_dir=profile_dir,
                        public_base_url="https://browser.example.com",
                    )

                for path in stale_paths:
                    self.assertFalse(path.exists(), f"{path.name} should be removed before startup")

        asyncio.run(run_test())

    def test_start_launches_x11vnc_with_nomodtweak(self) -> None:
        async def run_test() -> None:
            driver = PlaywrightBrowserDriver()

            with tempfile.TemporaryDirectory() as tmp:
                profile_dir = Path(tmp) / "profile"
                profile_dir.mkdir(parents=True, exist_ok=True)
                recorded_calls = []

                async def fake_create_subprocess_exec(*args, **kwargs):
                    recorded_calls.append(args)
                    return _FakeProcess()

                with patch(
                    "browser.driver.asyncio.create_subprocess_exec",
                    new=AsyncMock(side_effect=fake_create_subprocess_exec),
                ), patch.object(
                    driver,
                    "_wait_for_x11",
                    new=AsyncMock(),
                ), patch.object(
                    driver,
                    "_wait_for_port",
                    new=AsyncMock(),
                ), patch.object(
                    driver,
                    "_connect_playwright",
                    new=AsyncMock(return_value=(object(), object(), object(), object())),
                ):
                    await driver.start(
                        open_id="ou_test",
                        profile_dir=profile_dir,
                        public_base_url="https://browser.example.com",
                    )

                x11vnc_args = next(args for args in recorded_calls if args and args[0] == "x11vnc")
                self.assertIn("-nomodtweak", x11vnc_args)

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
