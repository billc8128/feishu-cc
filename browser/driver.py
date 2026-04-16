from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass
class _RunningSession:
    open_id: str
    viewer_token: str
    viewer_url: str
    xvfb: asyncio.subprocess.Process
    fluxbox: asyncio.subprocess.Process
    x11vnc: asyncio.subprocess.Process
    chromium: asyncio.subprocess.Process
    playwright: Any
    browser: Any
    context: Any
    page: Any


class PlaywrightBrowserDriver:
    def __init__(
        self,
        *,
        display: str = ":99",
        vnc_port: int = 5900,
        cdp_port: int = 9222,
        screen_size: str = "1440x960x24",
    ) -> None:
        self.display = display
        self.vnc_port = vnc_port
        self.cdp_port = cdp_port
        self.screen_size = screen_size
        self._running: Optional[_RunningSession] = None

    async def start(self, *, open_id: str, profile_dir: Path, public_base_url: str) -> Dict[str, Any]:
        if self._running:
            await self.stop(self._running.open_id)

        self._cleanup_profile_locks(profile_dir)

        env = os.environ.copy()
        env["DISPLAY"] = self.display
        env["HOME"] = str(profile_dir)

        xvfb = await asyncio.create_subprocess_exec(
            "Xvfb",
            self.display,
            "-screen",
            "0",
            self.screen_size,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        await self._wait_for_x11()

        fluxbox = await asyncio.create_subprocess_exec(
            "fluxbox",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        x11vnc = await asyncio.create_subprocess_exec(
            "x11vnc",
            "-display",
            self.display,
            "-rfbport",
            str(self.vnc_port),
            "-nomodtweak",
            "-forever",
            "-shared",
            "-localhost",
            "-nopw",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        chromium = await asyncio.create_subprocess_exec(
            "chromium",
            "--no-first-run",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={self.cdp_port}",
            f"--user-data-dir={profile_dir}",
            "--window-size=1440,960",
            "about:blank",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        try:
            await self._wait_for_port(self.vnc_port)
            await self._wait_for_port(self.cdp_port)
            playwright, browser, context, page = await self._connect_playwright()
        except Exception:
            await self._terminate_process(chromium)
            await self._terminate_process(x11vnc)
            await self._terminate_process(fluxbox)
            await self._terminate_process(xvfb)
            raise

        viewer_token = secrets.token_urlsafe(24)
        viewer_url = public_base_url.rstrip("/") + f"/view/{viewer_token}"
        self._running = _RunningSession(
            open_id=open_id,
            viewer_token=viewer_token,
            viewer_url=viewer_url,
            xvfb=xvfb,
            fluxbox=fluxbox,
            x11vnc=x11vnc,
            chromium=chromium,
            playwright=playwright,
            browser=browser,
            context=context,
            page=page,
        )
        return {"viewer_token": viewer_token, "viewer_url": viewer_url}

    async def stop(self, open_id: str) -> None:
        running = self._require_running(open_id)
        with contextlib.suppress(Exception):
            await running.browser.close()
        with contextlib.suppress(Exception):
            await running.playwright.stop()
        await self._terminate_process(running.chromium)
        await self._terminate_process(running.x11vnc)
        await self._terminate_process(running.fluxbox)
        await self._terminate_process(running.xvfb)
        self._running = None

    async def navigate(self, open_id: str, url: str) -> Dict[str, Any]:
        running = self._require_running(open_id)
        await running.page.goto(url, wait_until="domcontentloaded")
        return {"state": "active", "url": running.page.url}

    async def click(self, open_id: str, selector: str) -> Dict[str, Any]:
        running = self._require_running(open_id)
        await running.page.locator(selector).first.click(timeout=10_000)
        return {"state": "active", "selector": selector}

    async def type(self, open_id: str, selector: str, text: str, *, clear: bool) -> Dict[str, Any]:
        running = self._require_running(open_id)
        locator = running.page.locator(selector).first
        if clear:
            await locator.fill(text, timeout=10_000)
        else:
            await locator.type(text, timeout=10_000)
        return {"state": "active", "selector": selector}

    async def wait(
        self,
        open_id: str,
        *,
        selector: str,
        text: str,
        timeout_ms: int,
    ) -> Dict[str, Any]:
        running = self._require_running(open_id)
        if selector:
            await running.page.locator(selector).first.wait_for(timeout=timeout_ms)
        elif text:
            await running.page.get_by_text(text).first.wait_for(timeout=timeout_ms)
        else:
            await running.page.wait_for_load_state("networkidle", timeout=timeout_ms)
        return {"state": "active"}

    async def snapshot(self, open_id: str) -> Dict[str, Any]:
        running = self._require_running(open_id)
        snapshot = await running.page.evaluate(
            """
            () => {
              const clip = (value, max = 2000) => (value || "").replace(/\\s+/g, " ").trim().slice(0, max);
              const cssPath = (el) => {
                if (!el || !(el instanceof Element)) return "";
                if (el.id) return "#" + CSS.escape(el.id);
                const parts = [];
                let current = el;
                while (current && current.nodeType === Node.ELEMENT_NODE && parts.length < 5) {
                  let part = current.tagName.toLowerCase();
                  if (current.classList.length) {
                    part += "." + Array.from(current.classList).slice(0, 2).map((cls) => CSS.escape(cls)).join(".");
                  }
                  const parent = current.parentElement;
                  if (parent) {
                    const siblings = Array.from(parent.children).filter((node) => node.tagName === current.tagName);
                    if (siblings.length > 1) {
                      part += `:nth-of-type(${siblings.indexOf(current) + 1})`;
                    }
                  }
                  parts.unshift(part);
                  current = parent;
                }
                return parts.join(" > ");
              };

              return {
                title: document.title || "",
                url: location.href,
                text: clip(document.body ? document.body.innerText : ""),
                buttons: Array.from(document.querySelectorAll('button, [role="button"], input[type="button"], input[type="submit"]'))
                  .slice(0, 12)
                  .map((el) => ({ text: clip(el.innerText || el.value || el.getAttribute('aria-label') || '', 120), selector: cssPath(el) })),
                inputs: Array.from(document.querySelectorAll('input, textarea, select'))
                  .slice(0, 12)
                  .map((el) => ({ name: clip(el.getAttribute('name') || el.getAttribute('placeholder') || el.getAttribute('aria-label') || '', 120), selector: cssPath(el) })),
                links: Array.from(document.querySelectorAll('a[href]'))
                  .slice(0, 12)
                  .map((el) => ({ text: clip(el.innerText || el.getAttribute('aria-label') || '', 120), href: el.href, selector: cssPath(el) })),
              };
            }
            """
        )
        return {"state": "active", "snapshot": snapshot}

    def current_viewer_token(self) -> Optional[str]:
        return self._running.viewer_token if self._running else None

    async def _connect_playwright(self):
        from playwright.async_api import async_playwright

        playwright = await async_playwright().start()
        browser = await playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{self.cdp_port}")
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = context.pages[0] if context.pages else await context.new_page()
        await page.bring_to_front()
        return playwright, browser, context, page

    def _require_running(self, open_id: str) -> _RunningSession:
        if not self._running or self._running.open_id != open_id:
            raise RuntimeError("browser session is not running")
        return self._running

    async def _wait_for_x11(self) -> None:
        await asyncio.sleep(1.0)

    def _cleanup_profile_locks(self, profile_dir: Path) -> None:
        for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            path = profile_dir / name
            with contextlib.suppress(FileNotFoundError):
                path.unlink()

    async def _wait_for_port(self, port: int, *, timeout_seconds: float = 15.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
                return
            except Exception:
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError(f"port {port} did not become ready in time")
                await asyncio.sleep(0.2)

    async def _terminate_process(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
