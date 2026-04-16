import asyncio
import importlib
import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("ANTHROPIC_AUTH_TOKEN", "test-token")
os.environ.setdefault("FEISHU_APP_ID", "test-app-id")
os.environ.setdefault("FEISHU_APP_SECRET", "test-app-secret")
os.environ.setdefault("DATA_DIR", "/tmp/feishu-cc-test-data")


def _install_test_stubs() -> None:
    fake_apscheduler = types.ModuleType("apscheduler")
    fake_jobstores = types.ModuleType("apscheduler.jobstores")
    fake_sqlalchemy = types.ModuleType("apscheduler.jobstores.sqlalchemy")
    fake_schedulers = types.ModuleType("apscheduler.schedulers")
    fake_asyncio = types.ModuleType("apscheduler.schedulers.asyncio")
    fake_triggers = types.ModuleType("apscheduler.triggers")
    fake_cron = types.ModuleType("apscheduler.triggers.cron")

    class _DummyJobStore:
        def __init__(self, *args, **kwargs) -> None:
            pass

    class _DummyScheduler:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def add_job(self, *args, **kwargs) -> None:
            pass

        def remove_job(self, *args, **kwargs) -> None:
            pass

    class _DummyTrigger:
        @classmethod
        def from_crontab(cls, *args, **kwargs):
            return cls()

    fake_sqlalchemy.SQLAlchemyJobStore = _DummyJobStore
    fake_asyncio.AsyncIOScheduler = _DummyScheduler
    fake_cron.CronTrigger = _DummyTrigger

    fake_sdk = types.ModuleType("claude_agent_sdk")

    class _Dummy:
        def __init__(self, *args, **kwargs) -> None:
            pass

    fake_sdk.AssistantMessage = _Dummy
    fake_sdk.ClaudeAgentOptions = _Dummy
    fake_sdk.ClaudeSDKClient = _Dummy
    fake_sdk.ResultMessage = _Dummy
    fake_sdk.SystemMessage = _Dummy
    fake_sdk.TextBlock = _Dummy
    fake_sdk.ThinkingBlock = _Dummy
    fake_sdk.ToolResultBlock = _Dummy
    fake_sdk.ToolUseBlock = _Dummy

    fake_hooks = types.ModuleType("agent.hooks")
    fake_hooks.build_hooks = lambda open_id: {}

    fake_schedule = types.ModuleType("agent.tools_schedule")
    fake_schedule.build_schedule_mcp = lambda open_id: {}

    fake_deliver = types.ModuleType("agent.tools_deliver")
    fake_deliver.build_deliver_mcp = lambda open_id: {}

    sys.modules.setdefault("apscheduler", fake_apscheduler)
    sys.modules.setdefault("apscheduler.jobstores", fake_jobstores)
    sys.modules.setdefault("apscheduler.jobstores.sqlalchemy", fake_sqlalchemy)
    sys.modules.setdefault("apscheduler.schedulers", fake_schedulers)
    sys.modules.setdefault("apscheduler.schedulers.asyncio", fake_asyncio)
    sys.modules.setdefault("apscheduler.triggers", fake_triggers)
    sys.modules.setdefault("apscheduler.triggers.cron", fake_cron)
    sys.modules.setdefault("claude_agent_sdk", fake_sdk)
    sys.modules.setdefault("agent.hooks", fake_hooks)
    sys.modules.setdefault("agent.tools_schedule", fake_schedule)
    sys.modules.setdefault("agent.tools_deliver", fake_deliver)


_install_test_stubs()

from agent.run_context import get_current_task_context, use_task_context

runner = importlib.import_module("scheduler.runner")
project_state = importlib.import_module("project.state")


class SchedulerRunnerTests(unittest.TestCase):
    def test_fire_task_sets_scheduler_context_for_agent_run(self) -> None:
        async def run_test() -> None:
            task = types.SimpleNamespace(
                task_id="task-1",
                open_id="ou_1",
                project="project-a",
                prompt="run this",
                note=None,
            )
            seen_contexts = []

            async def fake_handle_user_message(open_id: str, text: str) -> None:
                seen_contexts.append(get_current_task_context())

            with patch.object(runner.store, "get_task", return_value=task), patch.object(
                runner.store, "runs_today_for_user", return_value=0
            ), patch.object(runner.store, "record_run"), patch.object(
                runner.feishu_client, "send_text", new=AsyncMock()
            ), patch(
                "agent.runner.handle_user_message",
                new=AsyncMock(side_effect=fake_handle_user_message),
            ):
                await runner.fire_task("task-1")

            self.assertEqual(len(seen_contexts), 1)
            self.assertEqual(seen_contexts[0].source, "scheduler")
            self.assertEqual(seen_contexts[0].task_id, "task-1")

        asyncio.run(run_test())

    def test_fire_task_restores_context_after_execution(self) -> None:
        async def run_test() -> None:
            task = types.SimpleNamespace(
                task_id="task-2",
                open_id="ou_2",
                project="project-b",
                prompt="run this too",
                note=None,
            )

            async def fake_handle_user_message(open_id: str, text: str) -> None:
                current = get_current_task_context()
                self.assertEqual(current.source, "scheduler")
                self.assertEqual(current.task_id, "task-2")

            with use_task_context(source="chat", task_id="outer-task"):
                outer_before = get_current_task_context()
                with patch.object(runner.store, "get_task", return_value=task), patch.object(
                    runner.store, "runs_today_for_user", return_value=0
                ), patch.object(runner.store, "record_run"), patch.object(
                    runner.feishu_client, "send_text", new=AsyncMock()
                ), patch.object(
                    project_state, "get_current_project", return_value="project-x"
                ), patch.object(
                    project_state, "set_current_project", new=Mock()
                ), patch(
                    "agent.runner.handle_user_message",
                    new=AsyncMock(side_effect=fake_handle_user_message),
                ):
                    await runner.fire_task("task-2")

                outer_after = get_current_task_context()

            self.assertEqual(outer_before.source, "chat")
            self.assertEqual(outer_before.task_id, "outer-task")
            self.assertEqual(outer_after.source, "chat")
            self.assertEqual(outer_after.task_id, "outer-task")

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
