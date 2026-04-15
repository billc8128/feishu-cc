import importlib
import os
import tempfile
import unittest

os.environ.setdefault("ANTHROPIC_AUTH_TOKEN", "test-token")
os.environ.setdefault("FEISHU_APP_ID", "test-app-id")
os.environ.setdefault("FEISHU_APP_SECRET", "test-app-secret")

from config import settings


class AuthStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._original_data_dir = settings.data_dir
        self._original_admin_ids = getattr(settings, "feishu_admin_open_ids", "")
        self._original_allowed_ids = settings.feishu_allowed_open_ids

    def tearDown(self) -> None:
        settings.data_dir = self._original_data_dir
        settings.feishu_allowed_open_ids = self._original_allowed_ids
        if hasattr(settings, "feishu_admin_open_ids"):
            settings.feishu_admin_open_ids = self._original_admin_ids

    def test_bootstraps_admin_and_legacy_allowed_users(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            settings.data_dir = data_dir
            settings.feishu_admin_open_ids = "ou_admin"
            settings.feishu_allowed_open_ids = "ou_seeded"

            auth_store = importlib.import_module("auth.store")
            auth_store._initialized = False

            admin = auth_store.get_user("ou_admin")
            seeded = auth_store.get_user("ou_seeded")

            self.assertEqual(admin.status, "approved")
            self.assertTrue(admin.is_admin)
            self.assertEqual(seeded.status, "approved")
            self.assertFalse(seeded.is_admin)

    def test_request_approve_reject_and_reapply(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            settings.data_dir = data_dir
            settings.feishu_admin_open_ids = "ou_admin"
            settings.feishu_allowed_open_ids = ""

            auth_store = importlib.import_module("auth.store")
            auth_store._initialized = False

            pending = auth_store.request_access("ou_guest")
            self.assertEqual(pending.status, "pending")

            rejected = auth_store.reject_user("ou_guest", "ou_admin", "名额已满")
            self.assertEqual(rejected.status, "rejected")
            self.assertEqual(rejected.review_note, "名额已满")

            repending = auth_store.request_access("ou_guest")
            self.assertEqual(repending.status, "pending")
            self.assertIsNone(repending.review_note)

            approved = auth_store.approve_user("ou_guest", "ou_admin")
            self.assertEqual(approved.status, "approved")
            self.assertEqual(approved.approved_by, "ou_admin")


if __name__ == "__main__":
    unittest.main()
