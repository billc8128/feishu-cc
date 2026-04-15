"""按 open_id 持久化用户的当前活动项目。

用 SQLite 而不是 JSON 文件,因为以后扩多用户时并发写会冲突。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from typing import Optional

from config import settings


logger = logging.getLogger(__name__)
_lock = threading.Lock()
_initialized = False


def _create_project_sessions_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS project_sessions (
            open_id TEXT NOT NULL,
            project_name TEXT NOT NULL,
            active_session_id TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (open_id, project_name)
        )
        """
    )


def _migrate_project_sessions_table(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table' AND name = 'project_sessions'
        """
    ).fetchone()
    if not row:
        _create_project_sessions_table(conn)
        return

    columns = {
        info[1] for info in conn.execute("PRAGMA table_info(project_sessions)").fetchall()
    }
    required = {"open_id", "project_name", "active_session_id", "updated_at"}
    if required.issubset(columns):
        return

    session_column = None
    if "active_session_id" in columns:
        session_column = "active_session_id"
    elif "session_id" in columns:
        session_column = "session_id"

    updated_expr = "updated_at" if "updated_at" in columns else "datetime('now')"
    legacy_name = "project_sessions_legacy"

    logger.warning(
        "migrating legacy project_sessions schema: columns=%s",
        sorted(columns),
    )
    conn.execute(f"DROP TABLE IF EXISTS {legacy_name}")
    conn.execute(f"ALTER TABLE project_sessions RENAME TO {legacy_name}")
    _create_project_sessions_table(conn)

    if {"open_id", "project_name"}.issubset(columns) and session_column:
        conn.execute(
            f"""
            INSERT OR REPLACE INTO project_sessions(
                open_id, project_name, active_session_id, updated_at
            )
            SELECT
                open_id,
                project_name,
                {session_column},
                {updated_expr}
            FROM {legacy_name}
            WHERE open_id IS NOT NULL
              AND project_name IS NOT NULL
              AND {session_column} IS NOT NULL
            """
        )

    conn.execute(f"DROP TABLE {legacy_name}")


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
                """
            )
            _migrate_project_sessions_table(conn)
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


def get_active_session_id(open_id: str, project_name: str) -> Optional[str]:
    with _conn() as c:
        row = c.execute(
            """
            SELECT active_session_id
            FROM project_sessions
            WHERE open_id = ? AND project_name = ?
            """,
            (open_id, project_name),
        ).fetchone()
    if not row:
        return None
    return row[0]


def set_active_session_id(open_id: str, project_name: str, session_id: str) -> None:
    with _conn() as c:
        c.execute(
            """
            INSERT INTO project_sessions(open_id, project_name, active_session_id, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(open_id, project_name) DO UPDATE SET
                active_session_id = excluded.active_session_id,
                updated_at = excluded.updated_at
            """,
            (open_id, project_name, session_id),
        )


def clear_active_session_id(open_id: str, project_name: str) -> None:
    with _conn() as c:
        c.execute(
            """
            DELETE FROM project_sessions
            WHERE open_id = ? AND project_name = ?
            """,
            (open_id, project_name),
        )
