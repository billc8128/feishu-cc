"""自定义 MCP 工具,让 Claude 能在飞书里创建/查看/删除定时任务。

每个 (open_id) 一个独立的 MCP server 实例,因为工具内部要绑定 open_id
做权限隔离(防止 Claude 让 A 的任务跑到 B 头上)。
"""
from __future__ import annotations

from typing import Any, Dict

from claude_agent_sdk import create_sdk_mcp_server, tool
from apscheduler.triggers.cron import CronTrigger

from project import state as project_state
from scheduler import store


def build_schedule_mcp(open_id: str):
    """为某个用户构造 schedule MCP server。"""

    @tool(
        "schedule_create",
        "Create a recurring scheduled task. The task will run as if the user "
        "sent the prompt at the scheduled time, and the result will be pushed "
        "to the user proactively. Use standard 5-field crontab syntax "
        "(minute hour day month weekday). Timezone is Asia/Shanghai. "
        "Returns the task_id on success.",
        {
            "cron": str,  # crontab 表达式,例如 "0 8 * * *"
            "prompt": str,  # 触发时要执行的指令
            "note": str,  # 任务的简短描述,用于列表展示
        },
    )
    async def schedule_create(args: Dict[str, Any]) -> Dict[str, Any]:
        cron_expr = args.get("cron", "").strip()
        prompt = args.get("prompt", "").strip()
        note = args.get("note", "").strip() or None

        if not cron_expr or not prompt:
            return {
                "content": [{"type": "text", "text": "Error: cron and prompt are required"}],
                "is_error": True,
            }

        # 校验 cron 表达式
        try:
            CronTrigger.from_crontab(cron_expr, timezone="Asia/Shanghai")
        except Exception as exc:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"Error: invalid crontab expression '{cron_expr}': {exc}",
                    }
                ],
                "is_error": True,
            }

        project = project_state.get_current_project(open_id)
        task = store.add_task(
            open_id=open_id,
            project=project,
            cron_expr=cron_expr,
            prompt=prompt,
            note=note,
        )
        store.schedule_job(task.task_id, cron_expr)

        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"✅ Created scheduled task #{task.task_id}\n"
                        f"  cron: {cron_expr}\n"
                        f"  project: {project}\n"
                        f"  note: {note or '(none)'}"
                    ),
                }
            ],
        }

    @tool(
        "schedule_list",
        "List all scheduled tasks for the current user.",
        {},
    )
    async def schedule_list(args: Dict[str, Any]) -> Dict[str, Any]:
        tasks = store.list_tasks(open_id)
        if not tasks:
            return {
                "content": [
                    {"type": "text", "text": "No scheduled tasks."}
                ]
            }
        lines = [f"You have {len(tasks)} scheduled task(s):"]
        for t in tasks:
            lines.append(
                f"  #{t.task_id}  cron={t.cron_expr}  project={t.project}  "
                f"note={t.note or '(none)'}"
            )
        return {
            "content": [{"type": "text", "text": "\n".join(lines)}]
        }

    @tool(
        "schedule_delete",
        "Delete a scheduled task by its task_id.",
        {"task_id": str},
    )
    async def schedule_delete(args: Dict[str, Any]) -> Dict[str, Any]:
        task_id = args.get("task_id", "").strip()
        if not task_id:
            return {
                "content": [{"type": "text", "text": "Error: task_id is required"}],
                "is_error": True,
            }
        ok = store.delete_task(task_id, open_id)
        if not ok:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"Task #{task_id} not found (or not yours).",
                    }
                ],
                "is_error": True,
            }
        store.unschedule_job(task_id)
        return {
            "content": [
                {"type": "text", "text": f"🗑 Deleted scheduled task #{task_id}"}
            ]
        }

    return create_sdk_mcp_server(
        name="schedule",
        version="1.0.0",
        tools=[schedule_create, schedule_list, schedule_delete],
    )
