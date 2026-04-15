"""访问控制存储。

用 SQLite 持久化用户的审批状态,并在启动/首次访问时从环境变量
引导管理员与历史白名单用户。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional

from config import settings

_lock = threading.Lock()
_initialized = False


@dataclass
class AccessUser:
    open_id: str
    status: str
    is_admin: bool
    requested_at: Optional[str]
    reviewed_at: Optional[str]
    approved_by: Optional[str]
    review_note: Optional[str]
    updated_at: str


def _row_to_user(row: tuple) -> AccessUser:
    return AccessUser(
        open_id=row[0],
        status=row[1],
        is_admin=bool(row[2]),
        requested_at=row[3],
        reviewed_at=row[4],
        approved_by=row[5],
        review_note=row[6],
        updated_at=row[7],
    )


def _ensure_schema() -> None:
    global _initialized
    if _initialized:
        return
    with _lock:
        if _initialized:
            return
        settings.ensure_dirs()
        with sqlite3.connect(settings.sqlite_path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS access_users (
                    open_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    requested_at TEXT,
                    reviewed_at TEXT,
                    approved_by TEXT,
                    review_note TEXT,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                """
            )
            _bootstrap_seed_users(conn)
        _initialized = True


def _bootstrap_seed_users(conn: sqlite3.Connection) -> None:
    for open_id in settings.admin_open_ids:
        conn.execute(
            """
            INSERT INTO access_users(
                open_id, status, is_admin, requested_at, reviewed_at, approved_by, review_note, updated_at
            )
            VALUES (?, 'approved', 1, datetime('now'), datetime('now'), ?, NULL, datetime('now'))
            ON CONFLICT(open_id) DO UPDATE SET
                status = 'approved',
                is_admin = 1,
                reviewed_at = datetime('now'),
                approved_by = excluded.approved_by,
                review_note = NULL,
                updated_at = datetime('now')
            """,
            (open_id, open_id),
        )

    admin_ids = set(settings.admin_open_ids)
    for open_id in settings.allowed_open_ids:
        if open_id in admin_ids:
            continue
        conn.execute(
            """
            INSERT INTO access_users(
                open_id, status, is_admin, requested_at, reviewed_at, approved_by, review_note, updated_at
            )
            VALUES (?, 'approved', 0, datetime('now'), datetime('now'), 'bootstrap', NULL, datetime('now'))
            ON CONFLICT(open_id) DO UPDATE SET
                status = 'approved',
                reviewed_at = datetime('now'),
                approved_by = excluded.approved_by,
                review_note = NULL,
                updated_at = datetime('now')
            """,
            (open_id,),
        )


@contextmanager
def _conn():
    _ensure_schema()
    conn = sqlite3.connect(settings.sqlite_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_user(open_id: str) -> Optional[AccessUser]:
    with _conn() as c:
        row = c.execute(
            """
            SELECT open_id, status, is_admin, requested_at, reviewed_at, approved_by, review_note, updated_at
            FROM access_users
            WHERE open_id = ?
            """,
            (open_id,),
        ).fetchone()
    if not row:
        return None
    return _row_to_user(row)


def list_admin_open_ids() -> list[str]:
    with _conn() as c:
        rows = c.execute(
            "SELECT open_id FROM access_users WHERE is_admin = 1 ORDER BY open_id"
        ).fetchall()
    return [row[0] for row in rows]


def get_access_status(open_id: str) -> str:
    user = get_user(open_id)
    if not user:
        return "unknown"
    if user.is_admin:
        return "approved"
    return user.status


def is_admin(open_id: str) -> bool:
    user = get_user(open_id)
    return bool(user and user.is_admin)


def request_access(open_id: str) -> AccessUser:
    existing = get_user(open_id)
    if existing and (existing.is_admin or existing.status in {"approved", "pending"}):
        return existing

    with _conn() as c:
        c.execute(
            """
            INSERT INTO access_users(
                open_id, status, is_admin, requested_at, reviewed_at, approved_by, review_note, updated_at
            )
            VALUES (?, 'pending', 0, datetime('now'), NULL, NULL, NULL, datetime('now'))
            ON CONFLICT(open_id) DO UPDATE SET
                status = 'pending',
                is_admin = access_users.is_admin,
                requested_at = datetime('now'),
                reviewed_at = NULL,
                approved_by = NULL,
                review_note = NULL,
                updated_at = datetime('now')
            """,
            (open_id,),
        )
    return get_user(open_id)  # type: ignore[return-value]


def approve_user(open_id: str, admin_open_id: str) -> AccessUser:
    with _conn() as c:
        c.execute(
            """
            INSERT INTO access_users(
                open_id, status, is_admin, requested_at, reviewed_at, approved_by, review_note, updated_at
            )
            VALUES (?, 'approved', 0, datetime('now'), datetime('now'), ?, NULL, datetime('now'))
            ON CONFLICT(open_id) DO UPDATE SET
                status = 'approved',
                requested_at = COALESCE(access_users.requested_at, datetime('now')),
                reviewed_at = datetime('now'),
                approved_by = excluded.approved_by,
                review_note = NULL,
                updated_at = datetime('now')
            """,
            (open_id, admin_open_id),
        )
    return get_user(open_id)  # type: ignore[return-value]


def reject_user(open_id: str, admin_open_id: str, note: Optional[str] = None) -> AccessUser:
    existing = get_user(open_id)
    if existing and existing.is_admin:
        return existing

    with _conn() as c:
        c.execute(
            """
            INSERT INTO access_users(
                open_id, status, is_admin, requested_at, reviewed_at, approved_by, review_note, updated_at
            )
            VALUES (?, 'rejected', 0, datetime('now'), datetime('now'), ?, ?, datetime('now'))
            ON CONFLICT(open_id) DO UPDATE SET
                status = 'rejected',
                requested_at = COALESCE(access_users.requested_at, datetime('now')),
                reviewed_at = datetime('now'),
                approved_by = excluded.approved_by,
                review_note = excluded.review_note,
                updated_at = datetime('now')
            """,
            (open_id, admin_open_id, note),
        )
    return get_user(open_id)  # type: ignore[return-value]
