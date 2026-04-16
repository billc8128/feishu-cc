"""浏览器 MCP 工具。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from claude_agent_sdk import create_sdk_mcp_server, tool

from agent import browser_approval
from agent import run_context
from agent.browser_client import BrowserPausedForTakeoverError, browser_client
from config import settings
from feishu.client import feishu_client
from scheduler import store as scheduler_store

logger = logging.getLogger(__name__)


async def _wait_until_session_ready(open_id: str) -> Dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + settings.browser_queue_wait_timeout_seconds
    while True:
        session = await browser_client.get_session(open_id)
        if session and session.get("state") in {"ready", "active"}:
            return session
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("browser session did not become ready in time")
        await asyncio.sleep(1.0)


def _tool_text(message: str, *, is_error: bool = False) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"content": [{"type": "text", "text": message}]}
    if is_error:
        payload["is_error"] = True
    return payload


def _takeover_pause_text() -> Dict[str, Any]:
    return _tool_text(
        "浏览器已交给你。处理完后点击浏览器页面里的 Resume Agent，我再继续。",
        is_error=True,
    )


def _tool_error(message: str, exc: Exception) -> Dict[str, Any]:
    if isinstance(exc, BrowserPausedForTakeoverError):
        return _takeover_pause_text()
    return _tool_text(f"{message}: {exc}", is_error=True)


def _approval_fallback_text(reason: str) -> str:
    return (
        "🌐 当前任务需要使用浏览器。\n"
        f"原因: {reason}\n"
        "允许: /browser yes\n"
        "拒绝: /browser no"
    )


def build_browser_mcp(open_id: str):
    @tool(
        "browser_open",
        "Open or reuse the current user's browser session. If no session exists, "
        "ask the user for browser permission in Feishu via approval card or "
        "/browser yes|no, then create or reuse the browser session. Returns the "
        "live viewer/takeover URL when ready. Use this before any other "
        "browser_* tool.",
        {"reason": str},
    )
    async def browser_open(args: Dict[str, Any]) -> Dict[str, Any]:
        reason = (args.get("reason") or "").strip() or "需要一个真实浏览器来继续操作"
        task_context = run_context.get_current_task_context()
        is_scheduled_task = task_context.source == "scheduler" and bool(task_context.task_id)
        is_trusted_scheduled_task = False
        if is_scheduled_task:
            try:
                is_trusted_scheduled_task = scheduler_store.is_browser_trusted(
                    task_context.task_id, open_id
                )
            except Exception:
                logger.warning(
                    "browser trust lookup failed; falling back to approval flow",
                    exc_info=True,
                    extra={"task_id": task_context.task_id, "open_id": open_id},
                )

        try:
            existing = await browser_client.get_session(open_id)
        except Exception as exc:
            return _tool_text(f"Browser service unavailable: {exc}", is_error=True)
        if existing and existing.get("state") in {"queued", "starting", "ready", "active"}:
            session = existing
        elif is_trusted_scheduled_task:
            try:
                session = await browser_client.ensure_session(open_id)
            except Exception as exc:
                return _tool_text(f"Browser service unavailable: {exc}", is_error=True)
        else:
            _, created = browser_approval.start_request(
                open_id,
                reason=reason,
                timeout_seconds=settings.browser_approval_timeout_seconds,
            )
            if created:
                card_kwargs: Dict[str, Any] = {"reason": reason}
                if is_scheduled_task:
                    card_kwargs["trust_note"] = "允许后，此定时任务后续将自动使用浏览器，不再重复询问。"
                card_message_id = await feishu_client.send_browser_approval_card(
                    open_id,
                    **card_kwargs,
                )
                if not card_message_id:
                    await feishu_client.send_text(open_id, _approval_fallback_text(reason))
            try:
                approved = await browser_approval.wait_for_decision(open_id)
            except browser_approval.ApprovalTimeoutError:
                return _tool_text("Browser request timed out waiting for user approval.", is_error=True)

            if not approved:
                return _tool_text("Browser request denied by user.", is_error=True)

            if is_scheduled_task and created:
                try:
                    scheduler_store.approve_browser_trust(task_context.task_id, open_id)
                except Exception:
                    logger.warning(
                        "browser trust persistence failed; continuing without stored trust",
                        exc_info=True,
                        extra={"task_id": task_context.task_id, "open_id": open_id},
                    )

            try:
                session = await browser_client.ensure_session(open_id)
            except Exception as exc:
                return _tool_text(f"Browser service unavailable: {exc}", is_error=True)

        state = session.get("state")
        if state == "queued":
            queue_position = session.get("queue_position", "?")
            await feishu_client.send_text(
                open_id,
                f"⏳ 浏览器排队中，前面还有 {queue_position} 个会话。轮到你后我会继续。",
            )
            try:
                session = await _wait_until_session_ready(open_id)
            except TimeoutError:
                return _tool_text("Browser session queue wait timed out.", is_error=True)
        elif state == "starting":
            try:
                session = await _wait_until_session_ready(open_id)
            except TimeoutError:
                return _tool_text("Browser session start timed out.", is_error=True)

        viewer_url = session.get("viewer_url", "")
        if viewer_url:
            await feishu_client.send_text(
                open_id,
                "👀 浏览器已就绪。你可以打开下面的旁观/接管链接实时查看 agent 的操作过程：\n"
                f"{viewer_url}",
            )

        return _tool_text(
            "Browser session ready.\n"
            f"state: {session.get('state', 'unknown')}\n"
            f"viewer_url: {viewer_url or '(none)'}"
        )

    @tool(
        "browser_navigate",
        "Navigate the current user's active browser session to a URL.",
        {"url": str},
    )
    async def browser_navigate(args: Dict[str, Any]) -> Dict[str, Any]:
        url = (args.get("url") or "").strip()
        if not url:
            return _tool_text("Error: url is required", is_error=True)
        try:
            result = await browser_client.navigate(open_id, url)
        except Exception as exc:
            return _tool_error("Browser navigate failed", exc)
        return _tool_text(f"Navigated browser to {result.get('url', url)}")

    @tool(
        "browser_click",
        "Click an element in the current user's active browser session using a CSS selector.",
        {"selector": str},
    )
    async def browser_click(args: Dict[str, Any]) -> Dict[str, Any]:
        selector = (args.get("selector") or "").strip()
        if not selector:
            return _tool_text("Error: selector is required", is_error=True)
        try:
            await browser_client.click(open_id, selector)
        except Exception as exc:
            return _tool_error("Browser click failed", exc)
        return _tool_text(f"Clicked selector: {selector}")

    @tool(
        "browser_type",
        "Type text into an element in the current user's active browser session.",
        {"selector": str, "text": str, "clear": bool},
    )
    async def browser_type(args: Dict[str, Any]) -> Dict[str, Any]:
        selector = (args.get("selector") or "").strip()
        text = args.get("text") or ""
        clear = bool(args.get("clear", True))
        if not selector:
            return _tool_text("Error: selector is required", is_error=True)
        try:
            await browser_client.type(open_id, selector, text, clear=clear)
        except Exception as exc:
            return _tool_error("Browser type failed", exc)
        return _tool_text(f"Typed into selector: {selector}")

    @tool(
        "browser_wait",
        "Wait for a selector, visible text, or general page readiness in the active browser session.",
        {"selector": str, "text": str, "timeout_ms": int},
    )
    async def browser_wait(args: Dict[str, Any]) -> Dict[str, Any]:
        selector = (args.get("selector") or "").strip()
        text = (args.get("text") or "").strip()
        timeout_ms = int(args.get("timeout_ms") or 10_000)
        try:
            await browser_client.wait(open_id, selector=selector, text=text, timeout_ms=timeout_ms)
        except Exception as exc:
            return _tool_error("Browser wait failed", exc)
        return _tool_text("Browser wait completed.")

    @tool(
        "browser_snapshot",
        "Return a text snapshot of the current page so the agent can inspect it.",
        {},
    )
    async def browser_snapshot(args: Dict[str, Any]) -> Dict[str, Any]:
        try:
            result = await browser_client.snapshot(open_id)
        except Exception as exc:
            return _tool_error("Browser snapshot failed", exc)
        snapshot = result.get("snapshot") or {}
        lines = [
            f"title: {snapshot.get('title', '')}",
            f"url: {snapshot.get('url', '')}",
            f"text: {snapshot.get('text', '')}",
        ]
        return _tool_text("\n".join(lines).strip())

    @tool(
        "browser_close",
        "Close the current user's browser session and release the worker.",
        {},
    )
    async def browser_close(args: Dict[str, Any]) -> Dict[str, Any]:
        try:
            result = await browser_client.close_session(open_id)
        except Exception as exc:
            return _tool_text(f"Browser close failed: {exc}", is_error=True)
        if not result:
            return _tool_text("No active browser session to close.", is_error=True)
        return _tool_text("Closed the browser session.")

    return create_sdk_mcp_server(
        name="browser",
        version="1.0.0",
        tools=[
            browser_open,
            browser_navigate,
            browser_click,
            browser_type,
            browser_wait,
            browser_snapshot,
            browser_close,
        ],
    )
