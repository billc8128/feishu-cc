import os
import sqlite3
import tempfile
import unittest

os.environ.setdefault("ANTHROPIC_AUTH_TOKEN", "test-token")
os.environ.setdefault("FEISHU_APP_ID", "test-app-id")
os.environ.setdefault("FEISHU_APP_SECRET", "test-app-secret")

from config import settings
from project import state as project_state


class ProjectStateMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._original_data_dir = settings.data_dir

    def tearDown(self) -> None:
        settings.data_dir = self._original_data_dir
        project_state._initialized = False

    def test_migrates_legacy_project_sessions_schema(self) -> None:
        open_id = "ou_test_user"
        project = "scratch"

        with tempfile.TemporaryDirectory() as data_dir:
            settings.data_dir = data_dir
            project_state._initialized = False

            settings.ensure_dirs()
            with sqlite3.connect(settings.sqlite_path) as conn:
                conn.executescript(
                    """
                    CREATE TABLE project_sessions (
                        open_id TEXT NOT NULL,
                        project_name TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                        PRIMARY KEY (open_id, project_name)
                    );

                    INSERT INTO project_sessions(open_id, project_name, session_id)
                    VALUES ('ou_test_user', 'scratch', 'legacy-session');
                    """
                )

            self.assertEqual(
                project_state.get_active_session_id(open_id, project),
                "legacy-session",
            )

            with sqlite3.connect(settings.sqlite_path) as conn:
                columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(project_sessions)")
                }
                row = conn.execute(
                    """
                    SELECT open_id, project_name, active_session_id
                    FROM project_sessions
                    WHERE open_id = ? AND project_name = ?
                    """,
                    (open_id, project),
                ).fetchone()

            self.assertIn("active_session_id", columns)
            self.assertNotIn("session_id", columns)
            self.assertEqual(row, (open_id, project, "legacy-session"))


if __name__ == "__main__":
    unittest.main()
