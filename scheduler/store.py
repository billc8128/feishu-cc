"""定时任务的元数据存储 + APScheduler 实例。

- APScheduler 用 SQLAlchemy job store 持久化到 SQLite(容器重启不丢)
- 任务详情(open_id / project / prompt)单独存在 schedule_tasks 表
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, date
from typing import List, Optional

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config import settings

logger = logging.getLogger(__name__)


# ---------- 元数据表(独立于 APScheduler 的 job store) ----------

_meta_lock = threading.Lock()
_meta_initialized = False


def _ensure_meta_schema() -> None:
    global _meta_initialized
    if _meta_initialized:
        return
    with _meta_lock:
        if _meta_initialized:
            return
        settings.ensure_dirs()
        with sqlite3.connect(settings.sqlite_path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schedule_tasks (
                    task_id TEXT PRIMARY KEY,
                    open_id TEXT NOT NULL,
                    project TEXT NOT NULL,
                    cron_expr TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    note TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS schedule_browser_trust (
                    task_id TEXT PRIMARY KEY,
                    open_id TEXT NOT NULL,
                    approved_at TEXT NOT NULL DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS schedule_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    open_id TEXT NOT NULL,
                    fired_at TEXT NOT NULL DEFAULT (datetime('now'))
                );

                CREATE INDEX IF NOT EXISTS idx_runs_user_date
                    ON schedule_runs(open_id, fired_at);
                """
            )
        _meta_initialized = True


@contextmanager
def _conn():
    _ensure_meta_schema()
    conn = sqlite3.connect(settings.sqlite_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------- 元数据 CRUD ----------

@dataclass
class ScheduledTask:
    task_id: str
    open_id: str
    project: str
    cron_expr: str
    prompt: str
    note: Optional[str]
    created_at: str


def add_task(
    open_id: str, project: str, cron_expr: str, prompt: str, note: Optional[str]
) -> ScheduledTask:
    task_id = str(uuid.uuid4())[:8]
    with _conn() as c:
        c.execute(
            """
            INSERT INTO schedule_tasks(task_id, open_id, project, cron_expr, prompt, note)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (task_id, open_id, project, cron_expr, prompt, note),
        )
    return ScheduledTask(
        task_id=task_id,
        open_id=open_id,
        project=project,
        cron_expr=cron_expr,
        prompt=prompt,
        note=note,
        created_at=datetime.utcnow().isoformat(),
    )


def get_task(task_id: str) -> Optional[ScheduledTask]:
    with _conn() as c:
        row = c.execute(
            "SELECT task_id, open_id, project, cron_expr, prompt, note, created_at "
            "FROM schedule_tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    if not row:
        return None
    return ScheduledTask(*row)


def list_tasks(open_id: str) -> List[ScheduledTask]:
    with _conn() as c:
        rows = c.execute(
            "SELECT task_id, open_id, project, cron_expr, prompt, note, created_at "
            "FROM schedule_tasks WHERE open_id = ? ORDER BY created_at",
            (open_id,),
        ).fetchall()
    return [ScheduledTask(*r) for r in rows]


def delete_task(task_id: str, open_id: str) -> bool:
    """删除任务(同时检查所有权,防止越权)。"""
    with _conn() as c:
        cur = c.execute(
            "DELETE FROM schedule_tasks WHERE task_id = ? AND open_id = ?",
            (task_id, open_id),
        )
        deleted = cur.rowcount > 0
        if deleted:
            c.execute(
                "DELETE FROM schedule_browser_trust WHERE task_id = ?",
                (task_id,),
            )
        return deleted


def is_browser_trusted(task_id: str, open_id: str) -> bool:
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM schedule_browser_trust WHERE task_id = ? AND open_id = ?",
            (task_id, open_id),
        ).fetchone()
    return row is not None


def approve_browser_trust(task_id: str, open_id: str) -> None:
    with _conn() as c:
        c.execute(
            """
            INSERT INTO schedule_browser_trust(task_id, open_id)
            SELECT task_id, open_id
            FROM schedule_tasks
            WHERE task_id = ? AND open_id = ?
            ON CONFLICT(task_id) DO UPDATE SET
                approved_at = datetime('now')
            WHERE schedule_browser_trust.open_id = excluded.open_id
            """,
            (task_id, open_id),
        )


def revoke_browser_trust(task_id: str, open_id: str) -> bool:
    with _conn() as c:
        cur = c.execute(
            "DELETE FROM schedule_browser_trust WHERE task_id = ? AND open_id = ?",
            (task_id, open_id),
        )
    return cur.rowcount > 0


def delete_browser_trust(task_id: str, open_id: str) -> None:
    with _conn() as c:
        c.execute(
            "DELETE FROM schedule_browser_trust WHERE task_id = ? AND open_id = ?",
            (task_id, open_id),
        )


def record_run(task_id: str, open_id: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO schedule_runs(task_id, open_id) VALUES (?, ?)",
            (task_id, open_id),
        )


def runs_today_for_user(open_id: str) -> int:
    today = date.today().isoformat()
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) FROM schedule_runs "
            "WHERE open_id = ? AND fired_at >= ?",
            (open_id, today),
        ).fetchone()
    return int(row[0]) if row else 0


# ---------- APScheduler 单例 ----------

_scheduler: Optional[AsyncIOScheduler] = None


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        settings.ensure_dirs()
        jobstore_url = f"sqlite:///{settings.data_path / 'scheduler.db'}"
        _scheduler = AsyncIOScheduler(
            jobstores={"default": SQLAlchemyJobStore(url=jobstore_url)},
            timezone="Asia/Shanghai",
        )
    return _scheduler


def schedule_job(task_id: str, cron_expr: str) -> None:
    """把任务挂到 APScheduler 上。job 函数是模块级函数,可序列化。"""
    from scheduler.runner import fire_task  # 模块级

    sched = get_scheduler()
    trigger = CronTrigger.from_crontab(cron_expr, timezone="Asia/Shanghai")
    sched.add_job(
        fire_task,
        trigger=trigger,
        args=[task_id],
        id=task_id,
        replace_existing=True,
        misfire_grace_time=300,
        coalesce=True,
    )


def unschedule_job(task_id: str) -> None:
    sched = get_scheduler()
    try:
        sched.remove_job(task_id)
    except Exception as exc:
        logger.warning("remove job %s failed: %s", task_id, exc)
