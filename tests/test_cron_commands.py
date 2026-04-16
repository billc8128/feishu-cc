import asyncio
import importlib
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("ANTHROPIC_AUTH_TOKEN", "test-token")
os.environ.setdefault("FEISHU_APP_ID", "test-app-id")
os.environ.setdefault("FEISHU_APP_SECRET", "test-app-secret")
os.environ.setdefault("DATA_DIR", "/tmp/feishu-cc-test-data")

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

sys.modules.setdefault("apscheduler", fake_apscheduler)
sys.modules.setdefault("apscheduler.jobstores", fake_jobstores)
sys.modules.setdefault("apscheduler.jobstores.sqlalchemy", fake_sqlalchemy)
sys.modules.setdefault("apscheduler.schedulers", fake_schedulers)
sys.modules.setdefault("apscheduler.schedulers.asyncio", fake_asyncio)
sys.modules.setdefault("apscheduler.triggers", fake_triggers)
sys.modules.setdefault("apscheduler.triggers.cron", fake_cron)


def _install_test_stubs() -> None:
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

    sys.modules.setdefault("claude_agent_sdk", fake_sdk)
    sys.modules.setdefault("agent.hooks", fake_hooks)
    sys.modules.setdefault("agent.tools_schedule", fake_schedule)
    sys.modules.setdefault("agent.tools_deliver", fake_deliver)


_install_test_stubs()
settings = importlib.import_module("config").settings
app_module = importlib.import_module("app")
auth_store = importlib.import_module("auth.store")
scheduler_store = importlib.import_module("scheduler.store")
from feishu.events import ParsedMessageEvent


class CronCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._original_data_dir = settings.data_dir
        self._original_admin_ids = getattr(settings, "feishu_admin_open_ids", "")
        settings.data_dir = self._tmp.name
        settings.feishu_admin_open_ids = ""
        auth_store._initialized = False
        scheduler_store._meta_initialized = False
        auth_store.approve_user("ou_user", "ou_admin")

    def tearDown(self) -> None:
        settings.data_dir = self._original_data_dir
        settings.feishu_admin_open_ids = self._original_admin_ids
        auth_store._initialized = False
        scheduler_store._meta_initialized = False
        self._tmp.cleanup()

    def test_cron_browser_revoke_removes_existing_trust(self) -> None:
        async def run_test() -> None:
            task = scheduler_store.add_task("ou_user", "project", "0 9 * * *", "prompt", None)
            scheduler_store.approve_browser_trust(task.task_id, "ou_user")
            parsed = ParsedMessageEvent(
                event_id="evt-1",
                sender_open_id="ou_user",
                chat_id="oc_1",
                chat_type="p2p",
                message_id="om_1",
                text=f"/cron browser revoke {task.task_id}",
                attachments=[],
            )

            with patch.object(app_module.feishu_client, "send_text", new=AsyncMock()) as send_text:
                await app_module._dispatch(parsed)

            self.assertFalse(scheduler_store.is_browser_trusted(task.task_id, "ou_user"))
            self.assertEqual(
                send_text.await_args.args[1],
                f"🧹 已撤销定时任务 #{task.task_id} 的浏览器自动授权。",
            )

        asyncio.run(run_test())

    def test_cron_browser_revoke_reports_missing_trust_when_absent(self) -> None:
        async def run_test() -> None:
            task = scheduler_store.add_task("ou_user", "project", "0 9 * * *", "prompt", None)
            parsed = ParsedMessageEvent(
                event_id="evt-2",
                sender_open_id="ou_user",
                chat_id="oc_1",
                chat_type="p2p",
                message_id="om_2",
                text=f"/cron browser revoke {task.task_id}",
                attachments=[],
            )

            with patch.object(app_module.feishu_client, "send_text", new=AsyncMock()) as send_text:
                await app_module._dispatch(parsed)

            self.assertEqual(
                send_text.await_args.args[1],
                f"未找到任务 #{task.task_id} 的浏览器授权。",
            )

        asyncio.run(run_test())

    def test_cron_browser_usage_error(self) -> None:
        async def run_test() -> None:
            parsed = ParsedMessageEvent(
                event_id="evt-3",
                sender_open_id="ou_user",
                chat_id="oc_1",
                chat_type="p2p",
                message_id="om_3",
                text="/cron browser",
                attachments=[],
            )

            with patch.object(app_module.feishu_client, "send_text", new=AsyncMock()) as send_text:
                await app_module._dispatch(parsed)

            self.assertEqual(send_text.await_args.args[1], "用法:/cron browser revoke <task_id>")

        asyncio.run(run_test())

    def test_cron_browser_revoke_without_task_id(self) -> None:
        async def run_test() -> None:
            parsed = ParsedMessageEvent(
                event_id="evt-4",
                sender_open_id="ou_user",
                chat_id="oc_1",
                chat_type="p2p",
                message_id="om_4",
                text="/cron browser revoke",
                attachments=[],
            )

            with patch.object(app_module.feishu_client, "send_text", new=AsyncMock()) as send_text:
                await app_module._dispatch(parsed)

            self.assertEqual(send_text.await_args.args[1], "用法:/cron browser revoke <task_id>")

        asyncio.run(run_test())

    def test_cron_delete_with_extra_tokens_is_rejected_and_keeps_task(self) -> None:
        async def run_test() -> None:
            task = scheduler_store.add_task("ou_user", "project", "0 9 * * *", "prompt", None)
            parsed = ParsedMessageEvent(
                event_id="evt-5",
                sender_open_id="ou_user",
                chat_id="oc_1",
                chat_type="p2p",
                message_id="om_5",
                text=f"/cron delete {task.task_id} extra",
                attachments=[],
            )

            with patch.object(app_module.feishu_client, "send_text", new=AsyncMock()) as send_text:
                await app_module._dispatch(parsed)

            self.assertIsNotNone(scheduler_store.get_task(task.task_id))
            self.assertEqual(send_text.await_args.args[1], "用法:/cron delete <task_id>")

        asyncio.run(run_test())

    def test_cron_help_includes_browser_revoke_command(self) -> None:
        async def run_test() -> None:
            parsed = ParsedMessageEvent(
                event_id="evt-6",
                sender_open_id="ou_user",
                chat_id="oc_1",
                chat_type="p2p",
                message_id="om_6",
                text="/cron help",
                attachments=[],
            )

            with patch.object(app_module.feishu_client, "send_text", new=AsyncMock()) as send_text:
                await app_module._dispatch(parsed)

            self.assertIn("/cron browser revoke <task_id>", send_text.await_args.args[1])

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
