"""定时任务触发时的执行逻辑。

被 APScheduler 在到点时回调。这里的函数必须是模块级,因为 APScheduler 的 job
持久化需要 import path。
"""
from __future__ import annotations

import logging

from config import settings
from feishu.client import feishu_client
from scheduler import store

logger = logging.getLogger(__name__)


async def fire_task(task_id: str) -> None:
    """APScheduler 到点时调用。"""
    task = store.get_task(task_id)
    if not task:
        logger.warning("fire_task: task %s not found", task_id)
        store.unschedule_job(task_id)
        return

    # 防失控:每天每用户最多 N 次
    runs_today = store.runs_today_for_user(task.open_id)
    if runs_today >= settings.schedule_daily_trigger_limit:
        logger.warning(
            "user %s exceeded daily trigger limit, skipping task %s",
            task.open_id,
            task.task_id,
        )
        await feishu_client.send_text(
            task.open_id,
            f"⚠️ 定时任务 #{task.task_id} 触发被跳过:今日定时任务总数已达上限"
            f"({settings.schedule_daily_trigger_limit})。可发 /cron list 查看。",
        )
        return

    store.record_run(task.task_id, task.open_id)

    # 通知用户:任务触发了
    await feishu_client.send_text(
        task.open_id,
        f"⏰ [定时任务 #{task.task_id}] 触发\n项目:{task.project}\n任务:{task.note or task.prompt[:80]}",
    )

    # 把 prompt 当作一条新消息喂给 agent
    # 注意:这里走 agent.runner.handle_user_message,会跑在用户当前会话里。
    # 缺点是会污染当前对话上下文。如果未来想隔离,可以单独建一个 ScheduledClient 池。
    from agent import runner as agent_runner

    # 切到任务指定的 project
    from project import state as project_state

    original_project = project_state.get_current_project(task.open_id)
    try:
        if task.project != original_project:
            project_state.set_current_project(task.open_id, task.project)
        await agent_runner.handle_user_message(task.open_id, task.prompt)
    except Exception as exc:
        logger.exception("scheduled task failed")
        await feishu_client.send_text(
            task.open_id,
            f"❌ 定时任务 #{task.task_id} 执行出错:{exc}",
        )
    finally:
        # 恢复用户原本的 project
        if task.project != original_project:
            project_state.set_current_project(task.open_id, original_project)


def restore_jobs_on_startup() -> None:
    """启动时把 SQLite 元数据里的所有任务重新挂到 APScheduler 上。

    APScheduler 自己也有 jobstore 持久化,但元数据库是 single source of truth。
    """
    tasks = []
    # 收集所有用户的所有任务(扫一遍元数据)
    import sqlite3

    settings.ensure_dirs()
    with sqlite3.connect(settings.sqlite_path) as conn:
        cur = conn.execute(
            "SELECT task_id, cron_expr FROM schedule_tasks"
        )
        for task_id, cron_expr in cur.fetchall():
            tasks.append((task_id, cron_expr))

    for task_id, cron_expr in tasks:
        try:
            store.schedule_job(task_id, cron_expr)
        except Exception as exc:
            logger.warning("restore job %s failed: %s", task_id, exc)

    logger.info("restored %d scheduled tasks", len(tasks))
