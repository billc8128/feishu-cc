"""浏览器授权确认状态。

首版使用进程内状态保存 pending/approved/denied 请求。
浏览器确认由飞书 `/browser yes|no` 命令驱动。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple


class ApprovalTimeoutError(TimeoutError):
    """等待浏览器授权超时。"""


@dataclass
class ApprovalRequest:
    open_id: str
    reason: str
    created_at: float
    expires_at: float
    decision: Optional[bool] = None
    event: asyncio.Event = field(default_factory=asyncio.Event)


_requests: Dict[str, ApprovalRequest] = {}


def reset_state() -> None:
    _requests.clear()


def _is_expired(request: ApprovalRequest, now: Optional[float] = None) -> bool:
    return (now or time.monotonic()) >= request.expires_at


def _cleanup_expired(now: Optional[float] = None) -> None:
    current = now or time.monotonic()
    expired_keys = [
        open_id for open_id, request in _requests.items() if _is_expired(request, current)
    ]
    for open_id in expired_keys:
        request = _requests.get(open_id)
        if request and request.decision is None:
            request.decision = None
            request.event.set()


def start_request(
    open_id: str,
    *,
    reason: str,
    timeout_seconds: int,
) -> Tuple[ApprovalRequest, bool]:
    _cleanup_expired()
    existing = _requests.get(open_id)
    if existing and not _is_expired(existing) and existing.decision is None:
        return existing, False

    request = ApprovalRequest(
        open_id=open_id,
        reason=reason,
        created_at=time.monotonic(),
        expires_at=time.monotonic() + timeout_seconds,
    )
    _requests[open_id] = request
    return request, True


def resolve_request(open_id: str, *, approved: bool) -> bool:
    _cleanup_expired()
    request = _requests.get(open_id)
    if not request or request.decision is not None or _is_expired(request):
        return False
    request.decision = approved
    request.event.set()
    return True


def get_request_status(open_id: str) -> str:
    request = _requests.get(open_id)
    if not request:
        return "none"
    if _is_expired(request):
        return "expired"
    if request.decision is True:
        return "approved"
    if request.decision is False:
        return "denied"
    return "pending"


async def wait_for_decision(open_id: str) -> bool:
    request = _requests.get(open_id)
    if not request:
        raise ApprovalTimeoutError(f"no pending request for {open_id}")

    timeout = max(0.0, request.expires_at - time.monotonic())
    try:
        await asyncio.wait_for(request.event.wait(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        _cleanup_expired()
        raise ApprovalTimeoutError(f"browser approval timed out for {open_id}") from exc

    status = get_request_status(open_id)
    if status == "expired":
        raise ApprovalTimeoutError(f"browser approval timed out for {open_id}")
    return bool(request.decision)
