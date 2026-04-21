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


class BrowserActionFailedError(BrowserServiceError):
    """浏览器服务返回的结构化业务错误(selector timeout / playwright error),带诊断字段。

    区别于 BrowserServiceError(基础设施类失败)——调用方应该把 screenshot_id
    取出来发给用户做诊断,然后换思路继续,而不是"服务挂了,放弃重试"。
    """

    def __init__(
        self,
        *,
        error_type: str,
        reason: str,
        page_url: str,
        screenshot_id: str,
        status_code: int = 422,
    ) -> None:
        super().__init__(reason, status_code=status_code)
        self.error_type = error_type
        self.reason = reason
        self.page_url = page_url
        self.screenshot_id = screenshot_id


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
        # 优先看 422 的结构化 detail(playwright 级业务错误)
        if response.status_code == 422:
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                detail = payload.get("detail")
                if isinstance(detail, dict):
                    raise BrowserActionFailedError(
                        error_type=str(detail.get("error_type") or "unknown"),
                        reason=str(detail.get("reason") or "unknown error"),
                        page_url=str(detail.get("page_url") or ""),
                        screenshot_id=str(detail.get("screenshot_id") or ""),
                        status_code=422,
                    )

        detail = self._error_detail(response)
        if response.status_code == 409 and detail == TAKEOVER_PAUSED_DETAIL:
            raise BrowserPausedForTakeoverError()
        raise BrowserServiceError(detail, status_code=response.status_code)

    async def fetch_failure_screenshot(self, screenshot_id: str) -> bytes:
        """下载失败截图的二进制 PNG。失败时抛 BrowserServiceError。"""
        if not screenshot_id:
            raise BrowserServiceError("empty screenshot_id", status_code=400)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(
                f"{self._base_url()}/v1/failures/{screenshot_id}",
                headers=self._headers(),
            )
        if response.status_code != 200:
            raise BrowserServiceError(
                f"fetch screenshot {screenshot_id}: HTTP {response.status_code}",
                status_code=response.status_code,
            )
        return response.content

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

    async def get_active_session(self) -> Optional[Dict[str, Any]]:
        return await self._request("GET", "/v1/sessions/active", allow_404=True)

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
