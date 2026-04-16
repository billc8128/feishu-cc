from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from browser.config import settings
from browser.driver import PlaywrightBrowserDriver
from browser.service import (
    BrowserSessionManager,
    NO_ACTIVE_SESSION_ERROR,
    TAKEOVER_PAUSED_ERROR,
)
from browser.viewer_page import render_viewer_page

_VNC_CLIENT_INPUT_MESSAGE_TYPES = {4, 5, 6}

settings.ensure_dirs()

driver = PlaywrightBrowserDriver()
manager = BrowserSessionManager(
    data_dir=settings.data_path,
    driver=driver,
    idle_timeout_seconds=settings.browser_idle_timeout_seconds,
    max_session_ttl_seconds=settings.browser_max_session_ttl_seconds,
)
app = FastAPI(title="browser-service", version="0.1.0")

novnc_dir = Path("/usr/share/novnc")
if novnc_dir.is_dir():
    app.mount("/novnc", StaticFiles(directory=novnc_dir), name="novnc")


class EnsureSessionRequest(BaseModel):
    open_id: str


class NavigateRequest(BaseModel):
    url: str


class ClickRequest(BaseModel):
    selector: str


class TypeRequest(BaseModel):
    selector: str
    text: str
    clear: bool = True


class WaitRequest(BaseModel):
    selector: str = ""
    text: str = ""
    timeout_ms: int = 10_000


def _require_auth(authorization: str = Header(default="")) -> None:
    expected = f"Bearer {settings.browser_service_token}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="unauthorized")


def _public_base_url(request: Request) -> str:
    return settings.browser_public_base_url.rstrip("/") or str(request.base_url).rstrip("/")


def _translate_session_runtime_error(error: RuntimeError) -> HTTPException:
    detail = str(error)
    if detail == NO_ACTIVE_SESSION_ERROR:
        return HTTPException(status_code=404, detail=detail)
    if detail == TAKEOVER_PAUSED_ERROR:
        return HTTPException(status_code=409, detail=detail)
    return HTTPException(status_code=409, detail=detail)


async def _run_browser_action(action) -> dict:
    try:
        return await action
    except RuntimeError as error:
        raise _translate_session_runtime_error(error) from error


def _public_viewer_control_payload(session: dict) -> dict:
    return {
        "state": session["state"],
        "controller": session["controller"],
        "paused_reason": session["paused_reason"],
        "last_control_change_at": session["last_control_change_at"],
    }


def _viewer_initial_status(session: dict) -> str:
    if session["controller"] == "human":
        return "Human takeover active. Agent control is paused."
    return "Viewer ready. Use Take Over to pause the agent or Resume Agent to hand control back."


def _viewer_message_payload(message: dict) -> bytes | None:
    if message.get("bytes") is not None:
        return message["bytes"]
    if message.get("text") is not None:
        return message["text"].encode("utf-8")
    return None


def _viewer_message_requires_human_control(payload: bytes) -> bool:
    return bool(payload) and payload[0] in _VNC_CLIENT_INPUT_MESSAGE_TYPES

@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.post("/v1/sessions/ensure", dependencies=[Depends(_require_auth)])
async def ensure_session(payload: EnsureSessionRequest, request: Request) -> dict:
    return await manager.ensure_session(payload.open_id, public_base_url=_public_base_url(request))


@app.get("/v1/sessions/active", dependencies=[Depends(_require_auth)])
async def get_active_session() -> dict:
    session = await manager.get_active_session()
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    return session


@app.get("/v1/sessions/{open_id}", dependencies=[Depends(_require_auth)])
async def get_session(open_id: str) -> dict:
    session = await manager.get_session(open_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    return session


@app.post("/v1/sessions/{open_id}/close", dependencies=[Depends(_require_auth)])
async def close_session(open_id: str, request: Request) -> dict:
    session = await manager.close_session(open_id, public_base_url=_public_base_url(request))
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    return session


@app.post("/v1/sessions/{open_id}/navigate", dependencies=[Depends(_require_auth)])
async def navigate(open_id: str, payload: NavigateRequest) -> dict:
    return await _run_browser_action(manager.navigate(open_id, payload.url))


@app.post("/v1/sessions/{open_id}/click", dependencies=[Depends(_require_auth)])
async def click(open_id: str, payload: ClickRequest) -> dict:
    return await _run_browser_action(manager.click(open_id, payload.selector))


@app.post("/v1/sessions/{open_id}/type", dependencies=[Depends(_require_auth)])
async def type_text(open_id: str, payload: TypeRequest) -> dict:
    return await _run_browser_action(
        manager.type(open_id, payload.selector, payload.text, clear=payload.clear)
    )


@app.post("/v1/sessions/{open_id}/wait", dependencies=[Depends(_require_auth)])
async def wait_for(open_id: str, payload: WaitRequest) -> dict:
    return await _run_browser_action(
        manager.wait(
            open_id,
            selector=payload.selector,
            text=payload.text,
            timeout_ms=payload.timeout_ms,
        )
    )


@app.post("/v1/sessions/{open_id}/snapshot", dependencies=[Depends(_require_auth)])
async def snapshot(open_id: str) -> dict:
    return await _run_browser_action(manager.snapshot(open_id))


@app.post("/v1/sessions/{open_id}/takeover", dependencies=[Depends(_require_auth)])
async def takeover_session(open_id: str) -> dict:
    try:
        return await manager.takeover(open_id)
    except RuntimeError as error:
        raise _translate_session_runtime_error(error) from error


@app.post("/v1/sessions/{open_id}/resume", dependencies=[Depends(_require_auth)])
async def resume_session(open_id: str) -> dict:
    try:
        return await manager.resume(open_id)
    except RuntimeError as error:
        raise _translate_session_runtime_error(error) from error


@app.post("/view/{viewer_token}/takeover")
async def takeover_viewer_session(viewer_token: str) -> dict:
    try:
        session = await manager.takeover_by_viewer_token(viewer_token)
        return _public_viewer_control_payload(session)
    except RuntimeError as error:
        raise _translate_session_runtime_error(error) from error


@app.post("/view/{viewer_token}/resume")
async def resume_viewer_session(viewer_token: str) -> dict:
    try:
        session = await manager.resume_by_viewer_token(viewer_token)
        return _public_viewer_control_payload(session)
    except RuntimeError as error:
        raise _translate_session_runtime_error(error) from error


@app.get("/view/{viewer_token}")
async def view_session(viewer_token: str) -> HTMLResponse:
    session = await manager.get_session_by_viewer_token(viewer_token)
    if not session:
        raise HTTPException(status_code=404, detail="viewer session not found")
    return HTMLResponse(
        render_viewer_page(
            viewer_token=session["viewer_token"],
            controller=session["controller"],
            status_text=_viewer_initial_status(session),
            interactive=session["controller"] == "human",
        )
    )


@app.websocket("/ws/{viewer_token}")
async def vnc_websocket(websocket: WebSocket, viewer_token: str) -> None:
    if not await manager.validate_viewer_token(viewer_token):
        await websocket.close(code=4403)
        return

    await websocket.accept()
    reader, writer = await asyncio.open_connection("127.0.0.1", driver.vnc_port)

    async def websocket_to_tcp() -> None:
        try:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
                payload = _viewer_message_payload(message)
                if payload is None:
                    continue
                if _viewer_message_requires_human_control(payload) and not await manager.can_viewer_interact(
                    viewer_token
                ):
                    continue
                writer.write(payload)
                await writer.drain()
                await manager.record_viewer_activity(viewer_token)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def tcp_to_websocket() -> None:
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                await websocket.send_bytes(data)
        finally:
            with contextlib.suppress(Exception):
                await websocket.close()

    tasks = [
        asyncio.create_task(websocket_to_tcp()),
        asyncio.create_task(tcp_to_websocket()),
    ]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    for task in done:
        with contextlib.suppress(Exception):
            await task
