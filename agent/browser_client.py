"""Bot -> browser service HTTP client."""
from __future__ import annotations

from typing import Any, Dict, Optional

import httpx

from config import settings

TAKEOVER_PAUSED_DETAIL = "BROWSER_PAUSED_FOR_TAKEOVER"


class BrowserServiceError(RuntimeError):
    def __init__(self, detail: str, *, status_code: int) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


class BrowserPausedForTakeoverError(BrowserServiceError):
    def __init__(self) -> None:
        super().__init__(TAKEOVER_PAUSED_DETAIL, status_code=409)


class BrowserServiceClient:
    def __init__(self) -> None:
        self._timeout = httpx.Timeout(30.0)

    def _base_url(self) -> str:
        base_url = settings.browser_service_base_url.rstrip("/")
        if not base_url:
            raise RuntimeError("browser service is not configured")
        return base_url

    def _headers(self) -> Dict[str, str]:
        token = settings.browser_service_token
        if not token:
            raise RuntimeError("browser service token is not configured")
        return {"Authorization": f"Bearer {token}"}

    def _error_detail(self, response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            payload = None

        if isinstance(payload, dict):
            detail = payload.get("detail")
            if isinstance(detail, str) and detail:
                return detail

        return response.text.strip() or f"browser service request failed: HTTP {response.status_code}"

    def _raise_for_error_response(self, response: httpx.Response) -> None:
        detail = self._error_detail(response)
        if response.status_code == 409 and detail == TAKEOVER_PAUSED_DETAIL:
            raise BrowserPausedForTakeoverError()
        raise BrowserServiceError(detail, status_code=response.status_code)

    async def _session_post(
        self,
        open_id: str,
        action: str,
        *,
        json: Optional[Dict[str, Any]] = None,
        allow_404: bool = False,
    ) -> Optional[Dict[str, Any]]:
        return await self._request(
            "POST",
            f"/v1/sessions/{open_id}/{action}",
            json=json,
            allow_404=allow_404,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[Dict[str, Any]] = None,
        allow_404: bool = False,
    ) -> Optional[Dict[str, Any]]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.request(
                method,
                f"{self._base_url()}{path}",
                json=json,
                headers=self._headers(),
            )
            if response.status_code == 404:
                if allow_404:
                    return None
                self._raise_for_error_response(response)
            if response.is_error:
                self._raise_for_error_response(response)
            return response.json()

    async def ensure_session(self, open_id: str) -> Dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/sessions/ensure",
            json={"open_id": open_id},
        ) or {}

    async def get_session(self, open_id: str) -> Optional[Dict[str, Any]]:
        return await self._request("GET", f"/v1/sessions/{open_id}", allow_404=True)

    async def close_session(self, open_id: str) -> Optional[Dict[str, Any]]:
        return await self._session_post(open_id, "close", allow_404=True)

    async def navigate(self, open_id: str, url: str) -> Dict[str, Any]:
        return await self._session_post(
            open_id,
            "navigate",
            json={"url": url},
        ) or {}

    async def click(self, open_id: str, selector: str) -> Dict[str, Any]:
        return await self._session_post(
            open_id,
            "click",
            json={"selector": selector},
        ) or {}

    async def type(
        self,
        open_id: str,
        selector: str,
        text: str,
        *,
        clear: bool = True,
    ) -> Dict[str, Any]:
        return await self._session_post(
            open_id,
            "type",
            json={"selector": selector, "text": text, "clear": clear},
        ) or {}

    async def wait(
        self,
        open_id: str,
        *,
        selector: str = "",
        text: str = "",
        timeout_ms: int = 10_000,
    ) -> Dict[str, Any]:
        return await self._session_post(
            open_id,
            "wait",
            json={"selector": selector, "text": text, "timeout_ms": timeout_ms},
        ) or {}

    async def snapshot(self, open_id: str) -> Dict[str, Any]:
        return await self._session_post(open_id, "snapshot") or {}

    async def takeover(self, open_id: str) -> Dict[str, Any]:
        return await self._session_post(open_id, "takeover") or {}

    async def resume(self, open_id: str) -> Dict[str, Any]:
        return await self._session_post(open_id, "resume") or {}


browser_client = BrowserServiceClient()
