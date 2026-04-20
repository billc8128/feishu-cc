"""飞书云文档 OAuth 授权流。

职责:
  - 构造授权 URL(带 state 防 CSRF)
  - 处理回调:state 校验 → code 换 token → 落库
  - get_valid_token:给业务方调用,自动刷新快过期的 token,
    per-open_id asyncio.Lock 单飞避免并发刷新被飞书废 refresh_token

不负责:
  - 飞书云文档 API(在 docs_client.py)
  - 业务层判断用户是否已授权(业务查 feishu_oauth_tokens 即可)

Spec 参考:docs/superpowers/specs/2026-04-20-feishu-docs-integration-design.md §4
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode

import httpx

from config import settings

logger = logging.getLogger(__name__)


# ---------- 常量 ----------

AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
SCOPES = "docx:document drive:file wiki:wiki offline_access"

STATE_TTL_SECONDS = 600  # 授权页 10 分钟不点同意就作废
REFRESH_PREEMPT_SECONDS = 300  # access token 剩余不足 5 分钟就提前刷新
TOKEN_REQUEST_TIMEOUT_S = 15


# ---------- 异常 ----------

class NotAuthorized(Exception):
    """用户未授权或授权已过期。业务方看到这个,提示用户 /auth-docs。"""


class OAuthStateInvalid(Exception):
    """回调里的 state 无效或过期。"""


class OAuthExchangeFailed(Exception):
    """code 换 token 失败(飞书返回错误或网络异常)。"""


# ---------- 数据类 ----------

@dataclass
class TokenRow:
    open_id: str
    access_token: str
    refresh_token: str
    access_expires_at: int
    refresh_expires_at: int
    docs_folder_token: Optional[str]
    docs_folder_name: str
    updated_at: int


# ---------- 初始化(schema + 锁) ----------

_schema_lock = threading.Lock()
_schema_ready = False

_refresh_locks: dict[str, asyncio.Lock] = {}
_locks_mutex = asyncio.Lock()


def _ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        settings.ensure_dirs()
        with sqlite3.connect(settings.sqlite_path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS feishu_oauth_tokens (
                    open_id             TEXT PRIMARY KEY,
                    access_token        TEXT NOT NULL,
                    refresh_token       TEXT NOT NULL,
                    access_expires_at   INTEGER NOT NULL,
                    refresh_expires_at  INTEGER NOT NULL,
                    docs_folder_token   TEXT,
                    docs_folder_name    TEXT DEFAULT 'AI 助手',
                    updated_at          INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS feishu_oauth_states (
                    state       TEXT PRIMARY KEY,
                    open_id     TEXT NOT NULL,
                    created_at  INTEGER NOT NULL,
                    expires_at  INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_oauth_states_expires
                    ON feishu_oauth_states(expires_at);
                """
            )
        _schema_ready = True


async def _get_refresh_lock(open_id: str) -> asyncio.Lock:
    async with _locks_mutex:
        if open_id not in _refresh_locks:
            _refresh_locks[open_id] = asyncio.Lock()
        return _refresh_locks[open_id]


# ---------- State 管理 ----------

def create_state(open_id: str) -> str:
    """生成 state 并入库,返回 state。"""
    _ensure_schema()
    state = secrets.token_urlsafe(32)
    now = int(time.time())
    with sqlite3.connect(settings.sqlite_path) as conn:
        # 顺手清掉过期 state,避免 DB 膨胀
        conn.execute("DELETE FROM feishu_oauth_states WHERE expires_at < ?", (now,))
        conn.execute(
            """
            INSERT INTO feishu_oauth_states(state, open_id, created_at, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (state, open_id, now, now + STATE_TTL_SECONDS),
        )
    logger.info("oauth: state created for open_id=...%s", open_id[-6:])
    return state


def consume_state(state: str) -> str:
    """校验并消耗 state,返回对应 open_id。一次性使用。"""
    _ensure_schema()
    now = int(time.time())
    with sqlite3.connect(settings.sqlite_path) as conn:
        row = conn.execute(
            "SELECT open_id, expires_at FROM feishu_oauth_states WHERE state = ?",
            (state,),
        ).fetchone()
        if not row:
            raise OAuthStateInvalid("state not found")
        open_id, expires_at = row
        # 一次性:立刻删
        conn.execute("DELETE FROM feishu_oauth_states WHERE state = ?", (state,))
        if expires_at < now:
            raise OAuthStateInvalid("state expired")
    return open_id


# ---------- 授权 URL ----------

def build_authorize_url(open_id: str) -> str:
    """给定用户,生成飞书授权页 URL。"""
    state = create_state(open_id)
    params = {
        "client_id": settings.feishu_app_id,
        "response_type": "code",
        "redirect_uri": _callback_url(),
        "scope": SCOPES,
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def _callback_url() -> str:
    """回调 URL。优先用 Railway 给的公网域名。"""
    base = getattr(settings, "public_base_url", "") or ""
    if not base:
        # 在 Railway 上 RAILWAY_PUBLIC_DOMAIN 总是存在
        import os
        rail = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
        if rail:
            base = f"https://{rail}"
    if not base:
        raise RuntimeError(
            "cannot determine public base URL for OAuth callback; "
            "set PUBLIC_BASE_URL env or run on Railway"
        )
    return f"{base.rstrip('/')}/feishu/oauth/callback"


# ---------- Token 存/取/刷新 ----------

def read_token(open_id: str) -> Optional[TokenRow]:
    _ensure_schema()
    with sqlite3.connect(settings.sqlite_path) as conn:
        row = conn.execute(
            """
            SELECT open_id, access_token, refresh_token,
                   access_expires_at, refresh_expires_at,
                   docs_folder_token, docs_folder_name, updated_at
            FROM feishu_oauth_tokens WHERE open_id = ?
            """,
            (open_id,),
        ).fetchone()
    if not row:
        return None
    return TokenRow(
        open_id=row[0], access_token=row[1], refresh_token=row[2],
        access_expires_at=row[3], refresh_expires_at=row[4],
        docs_folder_token=row[5], docs_folder_name=row[6] or "AI 助手",
        updated_at=row[7],
    )


def save_token(
    open_id: str,
    access_token: str,
    refresh_token: str,
    access_expires_at: int,
    refresh_expires_at: int,
) -> None:
    _ensure_schema()
    now = int(time.time())
    with sqlite3.connect(settings.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO feishu_oauth_tokens(
                open_id, access_token, refresh_token,
                access_expires_at, refresh_expires_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(open_id) DO UPDATE SET
                access_token = excluded.access_token,
                refresh_token = excluded.refresh_token,
                access_expires_at = excluded.access_expires_at,
                refresh_expires_at = excluded.refresh_expires_at,
                updated_at = excluded.updated_at
            """,
            (open_id, access_token, refresh_token,
             access_expires_at, refresh_expires_at, now),
        )
    logger.info(
        "oauth: token saved for ...%s (access_expires=%s refresh_expires=%s)",
        open_id[-6:],
        _fmt_ts(access_expires_at),
        _fmt_ts(refresh_expires_at),
    )


def save_folder_token(open_id: str, folder_token: str) -> None:
    _ensure_schema()
    with sqlite3.connect(settings.sqlite_path) as conn:
        conn.execute(
            "UPDATE feishu_oauth_tokens SET docs_folder_token = ? WHERE open_id = ?",
            (folder_token, open_id),
        )


def _fmt_ts(ts: int) -> str:
    """格式化 unix ts,仅用于日志。"""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(ts))
    except Exception:
        return str(ts)


async def exchange_code_for_token(code: str) -> dict:
    """用 code 换 access+refresh token。返回飞书原始 JSON data 字段。"""
    payload = {
        "grant_type": "authorization_code",
        "client_id": settings.feishu_app_id,
        "client_secret": settings.feishu_app_secret,
        "code": code,
        "redirect_uri": _callback_url(),
    }
    return await _post_token(payload)


async def _refresh(refresh_token: str) -> dict:
    payload = {
        "grant_type": "refresh_token",
        "client_id": settings.feishu_app_id,
        "client_secret": settings.feishu_app_secret,
        "refresh_token": refresh_token,
    }
    return await _post_token(payload)


async def _post_token(payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=TOKEN_REQUEST_TIMEOUT_S) as client:
        resp = await client.post(
            TOKEN_URL,
            json=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
    if resp.status_code >= 500:
        raise OAuthExchangeFailed(f"upstream {resp.status_code}")
    try:
        data = resp.json()
    except Exception as exc:
        raise OAuthExchangeFailed(f"non-json response: {exc}")
    # 飞书 v2 token 端点返回顶层 code/msg + access_token 等扁平字段
    if data.get("code") not in (0, None):
        # 日志里不打完整 payload(含 client_secret)
        raise OAuthExchangeFailed(f"feishu code={data.get('code')} msg={data.get('msg')}")
    return data


def _normalize_token_payload(data: dict) -> tuple[str, str, int, int]:
    """从飞书返回里提取 (access, refresh, access_exp, refresh_exp)。

    飞书 v2 语义:expires_in 秒,refresh_token_expires_in 秒。
    有些老文档是 expire,我们两个都兼容一下。
    """
    now = int(time.time())
    access = data["access_token"]
    refresh = data["refresh_token"]
    access_exp = now + int(
        data.get("expires_in") or data.get("expire") or 7200
    )
    refresh_exp = now + int(
        data.get("refresh_token_expires_in")
        or data.get("refresh_expires_in")
        or data.get("refresh_expire")
        or 30 * 24 * 3600
    )
    return access, refresh, access_exp, refresh_exp


async def complete_authorization(code: str, open_id: str) -> None:
    """回调 handler 用:拿 code 换 token 并落库。失败抛 OAuthExchangeFailed。"""
    data = await exchange_code_for_token(code)
    access, refresh, access_exp, refresh_exp = _normalize_token_payload(data)
    save_token(open_id, access, refresh, access_exp, refresh_exp)


async def get_valid_token(open_id: str) -> str:
    """业务方入口:拿一个当前有效的 access_token。

    会按需自动刷新(per-open_id 锁,双检)。
    未授权/refresh 也过期 → NotAuthorized。
    """
    row = read_token(open_id)
    if not row:
        raise NotAuthorized("no token for this user")

    now = int(time.time())
    if row.access_expires_at - now > REFRESH_PREEMPT_SECONDS:
        return row.access_token
    if row.refresh_expires_at <= now:
        raise NotAuthorized("refresh_token expired")

    lock = await _get_refresh_lock(open_id)
    async with lock:
        # 双检:等锁过程中可能已被别的协程刷过
        row = read_token(open_id)
        if not row:
            raise NotAuthorized("token disappeared during refresh")
        if row.access_expires_at - int(time.time()) > REFRESH_PREEMPT_SECONDS:
            return row.access_token
        data = await _refresh(row.refresh_token)
        access, refresh, access_exp, refresh_exp = _normalize_token_payload(data)
        save_token(open_id, access, refresh, access_exp, refresh_exp)
        return access


# ---------- Token 尾指纹(仅日志) ----------

def token_suffix(token: str) -> str:
    """返回最后 6 位用于日志定位,禁止打印完整 token。"""
    if not token:
        return "<empty>"
    return f"...{token[-6:]}"
