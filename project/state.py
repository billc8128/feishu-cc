"""按 (open_id, project_name) 持久化:当前活动项目、各项目的 session_id。

用 SQLite 而不是 JSON 文件,因为以后扩多用户时并发写会冲突。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Optional

from config import settings


_lock = threading.Lock()
_initialized = False


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
                CREATE TABLE IF NOT EXISTS current_project (
                    open_id TEXT PRIMARY KEY,
                    project_name TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS project_sessions (
                    open_id TEXT NOT NULL,
                    project_name TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (open_id, project_name)
                );
                """
            )
        _initialized = True


@contextmanager
def _conn():
    _ensure_schema()
    conn = sqlite3.connect(settings.sqlite_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------- 当前项目 ----------

def get_current_project(open_id: str) -> str:
    """返回用户当前活动项目名;首次访问返回 'scratch' 并落库。"""
    with _conn() as c:
        row = c.execute(
            "SELECT project_name FROM current_project WHERE open_id = ?",
            (open_id,),
        ).fetchone()
        if row:
            return row[0]
        c.execute(
            "INSERT INTO current_project(open_id, project_name) VALUES (?, ?)",
            (open_id, "scratch"),
        )
        return "scratch"


def set_current_project(open_id: str, project_name: str) -> None:
    with _conn() as c:
        c.execute(
            """
            INSERT INTO current_project(open_id, project_name, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(open_id) DO UPDATE SET
                project_name = excluded.project_name,
                updated_at = excluded.updated_at
            """,
            (open_id, project_name),
        )


# ---------- session_id 持久化 ----------

def get_session_id(open_id: str, project_name: str) -> Optional[str]:
    with _conn() as c:
        row = c.execute(
            "SELECT session_id FROM project_sessions WHERE open_id = ? AND project_name = ?",
            (open_id, project_name),
        ).fetchone()
        return row[0] if row else None


def set_session_id(open_id: str, project_name: str, session_id: str) -> None:
    with _conn() as c:
        c.execute(
            """
            INSERT INTO project_sessions(open_id, project_name, session_id, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(open_id, project_name) DO UPDATE SET
                session_id = excluded.session_id,
                updated_at = excluded.updated_at
            """,
            (open_id, project_name, session_id),
        )


def clear_session_id(open_id: str, project_name: str) -> None:
    with _conn() as c:
        c.execute(
            "DELETE FROM project_sessions WHERE open_id = ? AND project_name = ?",
            (open_id, project_name),
        )
