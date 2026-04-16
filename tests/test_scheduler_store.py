import importlib
import os
import sys
import types
import tempfile
import sqlite3
import unittest

os.environ.setdefault("ANTHROPIC_AUTH_TOKEN", "test-token")
os.environ.setdefault("FEISHU_APP_ID", "test-app-id")
os.environ.setdefault("FEISHU_APP_SECRET", "test-app-secret")

from config import settings


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


store = importlib.import_module("scheduler.store")


class SchedulerStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._original_data_dir = settings.data_dir

    def tearDown(self) -> None:
        settings.data_dir = self._original_data_dir
        store._meta_initialized = False

    def test_approve_browser_trust_for_owned_task(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            settings.data_dir = data_dir
            store._meta_initialized = False

            task = store.add_task("ou_owner", "project", "0 9 * * *", "prompt", None)
            self.assertFalse(store.is_browser_trusted(task.task_id, "ou_owner"))

            store.approve_browser_trust(task.task_id, "ou_owner")

            self.assertTrue(store.is_browser_trusted(task.task_id, "ou_owner"))

    def test_approve_browser_trust_rejects_other_users_task(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            settings.data_dir = data_dir
            store._meta_initialized = False

            task = store.add_task("ou_owner", "project", "0 9 * * *", "prompt", None)

            store.approve_browser_trust(task.task_id, "ou_other")

            self.assertFalse(store.is_browser_trusted(task.task_id, "ou_other"))
            self.assertFalse(store.is_browser_trusted(task.task_id, "ou_owner"))

    def test_approve_browser_trust_rejects_nonexistent_task(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            settings.data_dir = data_dir
            store._meta_initialized = False

            store.approve_browser_trust("missing-task", "ou_owner")

            self.assertFalse(store.is_browser_trusted("missing-task", "ou_owner"))

    def test_revoke_is_owner_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            settings.data_dir = data_dir
            store._meta_initialized = False

            task = store.add_task("ou_owner", "project", "0 9 * * *", "prompt", None)
            store.approve_browser_trust(task.task_id, "ou_owner")

            self.assertFalse(store.revoke_browser_trust(task.task_id, "ou_other"))
            self.assertTrue(store.is_browser_trusted(task.task_id, "ou_owner"))

            self.assertTrue(store.revoke_browser_trust(task.task_id, "ou_owner"))
            self.assertFalse(store.is_browser_trusted(task.task_id, "ou_owner"))

    def test_delete_task_removes_browser_trust_even_with_bad_owner_data(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            settings.data_dir = data_dir
            store._meta_initialized = False

            task = store.add_task("ou_owner", "project", "0 9 * * *", "prompt", None)

            with sqlite3.connect(settings.sqlite_path) as conn:
                conn.execute(
                    """
                    INSERT INTO schedule_browser_trust(task_id, open_id)
                    VALUES (?, ?)
                    """,
                    (task.task_id, "ou_bad"),
                )

            self.assertTrue(store.delete_task(task.task_id, "ou_owner"))
            self.assertFalse(store.is_browser_trusted(task.task_id, "ou_bad"))
            self.assertFalse(store.is_browser_trusted(task.task_id, "ou_owner"))


if __name__ == "__main__":
    unittest.main()
