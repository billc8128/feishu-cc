from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Deque, Dict, Optional, Protocol

ACTIVE_SESSION_STATE = "active"
QUEUED_SESSION_STATE = "queued"
CLOSED_SESSION_STATE = "closed"
AGENT_CONTROLLER = "agent"
HUMAN_CONTROLLER = "human"
TAKEOVER_PAUSED_REASON = "takeover"
TAKEOVER_PAUSED_ERROR = "BROWSER_PAUSED_FOR_TAKEOVER"
NO_ACTIVE_SESSION_ERROR = "no active browser session for this user"


class BrowserDriver(Protocol):
    async def start(self, *, open_id: str, profile_dir: Path, public_base_url: str) -> Dict[str, Any]:
        ...

    async def stop(self, open_id: str) -> None:
        ...

    async def navigate(self, open_id: str, url: str) -> Dict[str, Any]:
        ...

    async def click(self, open_id: str, selector: str) -> Dict[str, Any]:
        ...

    async def type(self, open_id: str, selector: str, text: str, *, clear: bool) -> Dict[str, Any]:
        ...

    async def wait(
        self,
        open_id: str,
        *,
        selector: str,
        text: str,
        timeout_ms: int,
    ) -> Dict[str, Any]:
        ...

    async def snapshot(self, open_id: str) -> Dict[str, Any]:
        ...


@dataclass
class SessionRecord:
    open_id: str
    state: str
    profile_dir: Path
    public_base_url: str
    created_at: float
    last_used_at: float
    controller: str = AGENT_CONTROLLER
    paused_reason: str = ""
    last_control_change_at: float = 0.0
    viewer_token: str = ""
    viewer_url: str = ""


class BrowserSessionManager:
    def __init__(
        self,
        *,
        data_dir: Path,
        driver: BrowserDriver,
        idle_timeout_seconds: int,
        max_session_ttl_seconds: int,
    ) -> None:
        self._data_dir = data_dir
        self._profiles_dir = data_dir / "browser-profiles"
        self._profiles_dir.mkdir(parents=True, exist_ok=True)
        self._driver = driver
        self._idle_timeout_seconds = idle_timeout_seconds
        self._max_session_ttl_seconds = max_session_ttl_seconds
        self._active_open_id: Optional[str] = None
        self._sessions: Dict[str, SessionRecord] = {}
        self._queue: Deque[str] = deque()
        self._lock = asyncio.Lock()

    async def ensure_session(self, open_id: str, *, public_base_url: str) -> Dict[str, Any]:
        async with self._lock:
            await self._expire_if_needed_locked(public_base_url)

            existing = self._sessions.get(open_id)
            if existing:
                if existing.state == QUEUED_SESSION_STATE:
                    return self._serialize(existing)
                if self._active_open_id == open_id:
                    existing.last_used_at = time.monotonic()
                    return self._serialize(existing)

            if self._active_open_id and self._active_open_id != open_id:
                if open_id not in self._queue:
                    record = SessionRecord(
                        open_id=open_id,
                        state=QUEUED_SESSION_STATE,
                        profile_dir=self._profile_dir(open_id),
                        public_base_url=public_base_url,
                        created_at=time.monotonic(),
                        last_used_at=time.monotonic(),
                    )
                    self._sessions[open_id] = record
                    self._queue.append(open_id)
                return self._serialize(self._sessions[open_id])

            record = await self._start_session_locked(open_id, public_base_url)
            return self._serialize(record)

    async def get_session(self, open_id: str) -> Optional[Dict[str, Any]]:
        async with self._lock:
            record = self._sessions.get(open_id)
            if not record:
                return None
            return self._serialize(record)

    async def takeover(self, open_id: str) -> Dict[str, Any]:
        async with self._lock:
            await self._expire_active_session_for_open_id_locked(open_id)
            record = self._require_active_locked(open_id)
            if record.controller == HUMAN_CONTROLLER:
                return self._serialize(record)
            now = time.monotonic()
            record.last_used_at = now
            record.controller = HUMAN_CONTROLLER
            record.paused_reason = TAKEOVER_PAUSED_REASON
            record.last_control_change_at = now
            return self._serialize(record)

    async def resume(self, open_id: str) -> Dict[str, Any]:
        async with self._lock:
            await self._expire_active_session_for_open_id_locked(open_id)
            record = self._require_active_locked(open_id)
            if record.controller == AGENT_CONTROLLER:
                return self._serialize(record)
            now = time.monotonic()
            record.last_used_at = now
            record.controller = AGENT_CONTROLLER
            record.paused_reason = ""
            record.last_control_change_at = now
            return self._serialize(record)

    async def navigate(self, open_id: str, url: str) -> Dict[str, Any]:
        async with self._lock:
            record = self._require_active_locked(open_id)
            self._require_agent_control_locked(record)
            record.last_used_at = time.monotonic()
            return await self._driver.navigate(open_id, url)

    async def click(self, open_id: str, selector: str) -> Dict[str, Any]:
        async with self._lock:
            record = self._require_active_locked(open_id)
            self._require_agent_control_locked(record)
            record.last_used_at = time.monotonic()
            return await self._driver.click(open_id, selector)

    async def type(self, open_id: str, selector: str, text: str, *, clear: bool) -> Dict[str, Any]:
        async with self._lock:
            record = self._require_active_locked(open_id)
            self._require_agent_control_locked(record)
            record.last_used_at = time.monotonic()
            return await self._driver.type(open_id, selector, text, clear=clear)

    async def wait(
        self,
        open_id: str,
        *,
        selector: str = "",
        text: str = "",
        timeout_ms: int = 10_000,
    ) -> Dict[str, Any]:
        async with self._lock:
            record = self._require_active_locked(open_id)
            self._require_agent_control_locked(record)
            record.last_used_at = time.monotonic()
            return await self._driver.wait(
                open_id,
                selector=selector,
                text=text,
                timeout_ms=timeout_ms,
            )

    async def snapshot(self, open_id: str) -> Dict[str, Any]:
        async with self._lock:
            record = self._require_active_locked(open_id)
            self._require_agent_control_locked(record)
            record.last_used_at = time.monotonic()
            return await self._driver.snapshot(open_id)

    async def validate_viewer_token(self, viewer_token: str) -> bool:
        async with self._lock:
            if not self._active_open_id:
                return False
            record = self._sessions.get(self._active_open_id)
            return bool(
                record and record.viewer_token == viewer_token and record.state == ACTIVE_SESSION_STATE
            )

    async def close_session(self, open_id: str, *, public_base_url: str) -> Optional[Dict[str, Any]]:
        async with self._lock:
            return await self._close_session_locked(open_id, public_base_url)

    async def _close_session_locked(self, open_id: str, public_base_url: str) -> Optional[Dict[str, Any]]:
        record = self._sessions.get(open_id)
        if not record:
            return None

        if record.state == QUEUED_SESSION_STATE:
            self._queue = deque(
                queued_open_id for queued_open_id in self._queue if queued_open_id != open_id
            )
            record.state = CLOSED_SESSION_STATE
            self._sessions.pop(open_id, None)
            return self._serialize(record)

        if self._active_open_id != open_id:
            return None

        await self._driver.stop(open_id)
        self._active_open_id = None
        record.state = CLOSED_SESSION_STATE
        closed = self._serialize(record)
        self._sessions.pop(open_id, None)
        if self._queue:
            next_open_id = self._queue.popleft()
            await self._start_session_locked(next_open_id, public_base_url)
        return closed

    async def _start_session_locked(self, open_id: str, public_base_url: str) -> SessionRecord:
        profile_dir = self._profile_dir(open_id)
        driver_result = await self._driver.start(
            open_id=open_id,
            profile_dir=profile_dir,
            public_base_url=public_base_url,
        )
        record = SessionRecord(
            open_id=open_id,
            state=ACTIVE_SESSION_STATE,
            profile_dir=profile_dir,
            public_base_url=public_base_url,
            created_at=time.monotonic(),
            last_used_at=time.monotonic(),
            viewer_token=str(driver_result.get("viewer_token", "")),
            viewer_url=str(driver_result.get("viewer_url", "")),
        )
        self._sessions[open_id] = record
        self._active_open_id = open_id
        return record

    def _profile_dir(self, open_id: str) -> Path:
        profile_dir = self._profiles_dir / open_id
        profile_dir.mkdir(parents=True, exist_ok=True)
        return profile_dir

    async def _expire_if_needed_locked(self, public_base_url: str) -> None:
        if not self._active_open_id:
            return
        record = self._sessions.get(self._active_open_id)
        if not record:
            self._active_open_id = None
            return
        now = time.monotonic()
        idle_expired = (now - record.last_used_at) >= self._idle_timeout_seconds
        ttl_expired = (now - record.created_at) >= self._max_session_ttl_seconds
        if idle_expired or ttl_expired:
            await self._close_session_locked(record.open_id, public_base_url)

    def _serialize(self, record: SessionRecord) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "open_id": record.open_id,
            "state": record.state,
            "controller": record.controller,
            "paused_reason": record.paused_reason,
            "last_control_change_at": record.last_control_change_at,
            "viewer_url": record.viewer_url,
            "viewer_token": record.viewer_token,
        }
        if record.state == QUEUED_SESSION_STATE:
            payload["queue_position"] = self._queue_position(record.open_id)
        return payload

    def _queue_position(self, open_id: str) -> int:
        for index, queued_open_id in enumerate(self._queue, start=1):
            if queued_open_id == open_id:
                return index
        return 0

    def _require_active_locked(self, open_id: str) -> SessionRecord:
        record = self._sessions.get(open_id)
        if not record or self._active_open_id != open_id or record.state != ACTIVE_SESSION_STATE:
            raise RuntimeError(NO_ACTIVE_SESSION_ERROR)
        return record

    def _require_agent_control_locked(self, record: SessionRecord) -> None:
        if record.controller != AGENT_CONTROLLER:
            raise RuntimeError(TAKEOVER_PAUSED_ERROR)

    async def _expire_active_session_for_open_id_locked(self, open_id: str) -> None:
        if self._active_open_id != open_id:
            return
        record = self._sessions.get(open_id)
        if not record:
            self._active_open_id = None
            return
        if self._session_is_expired_locked(record):
            await self._close_session_locked(open_id, record.public_base_url)

    def _session_is_expired_locked(self, record: SessionRecord) -> bool:
        now = time.monotonic()
        idle_expired = (now - record.last_used_at) >= self._idle_timeout_seconds
        ttl_expired = (now - record.created_at) >= self._max_session_ttl_seconds
        return idle_expired or ttl_expired
