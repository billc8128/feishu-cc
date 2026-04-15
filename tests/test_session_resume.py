import importlib
import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault("ANTHROPIC_AUTH_TOKEN", "test-token")
os.environ.setdefault("FEISHU_APP_ID", "test-app-id")
os.environ.setdefault("FEISHU_APP_SECRET", "test-app-secret")


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

    fake_feishu_client = types.ModuleType("feishu.client")
    fake_feishu_client.feishu_client = object()

    sys.modules.setdefault("claude_agent_sdk", fake_sdk)
    sys.modules.setdefault("agent.hooks", fake_hooks)
    sys.modules.setdefault("agent.tools_schedule", fake_schedule)
    sys.modules.setdefault("agent.tools_deliver", fake_deliver)
    sys.modules.setdefault("feishu.client", fake_feishu_client)


_install_test_stubs()
runner = importlib.import_module("agent.runner")
project_state = importlib.import_module("project.state")
settings = importlib.import_module("config").settings


class SessionResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._original_data_dir = settings.data_dir

    def tearDown(self) -> None:
        settings.data_dir = self._original_data_dir
        project_state._initialized = False

    def test_latest_session_id_uses_exact_encoded_dir(self) -> None:
        project_root = "/data/sandbox/users/ou_test_user/scratch"
        encoded = "-data-sandbox-users-ou_test_user-scratch"

        with tempfile.TemporaryDirectory() as home:
            projects_dir = Path(home) / ".claude" / "projects" / encoded
            projects_dir.mkdir(parents=True)

            older = projects_dir / "older-session.jsonl"
            older.write_text("{}", encoding="utf-8")
            time.sleep(0.01)
            newer = projects_dir / "newer-session.jsonl"
            newer.write_text("{}", encoding="utf-8")

            with patch.dict(os.environ, {"HOME": home}, clear=False):
                self.assertEqual(
                    runner._latest_session_id_for_cwd(project_root),
                    "newer-session",
                )

    def test_latest_session_id_handles_cli_normalized_dir_name(self) -> None:
        project_root = "/data/sandbox/users/ou_a45db68d728435acdd6415a0bac617b2/scratch"
        cli_encoded = (
            "-data-sandbox-users-ou-a45db68d728435acdd6415a0bac617b2-scratch"
        )

        with tempfile.TemporaryDirectory() as home:
            projects_dir = Path(home) / ".claude" / "projects" / cli_encoded
            projects_dir.mkdir(parents=True)
            expected = projects_dir / "restored-session.jsonl"
            expected.write_text("{}", encoding="utf-8")

            with patch.dict(os.environ, {"HOME": home}, clear=False):
                self.assertEqual(
                    runner._latest_session_id_for_cwd(project_root),
                    "restored-session",
                )

    def test_resume_session_prefers_persisted_active_session(self) -> None:
        open_id = "ou_test_user"
        project = "scratch"
        project_root = f"/data/sandbox/users/{open_id}/{project}"
        encoded = "-data-sandbox-users-ou_test_user-scratch"

        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as data_dir:
            settings.data_dir = data_dir
            project_state._initialized = False

            projects_dir = Path(home) / ".claude" / "projects" / encoded
            projects_dir.mkdir(parents=True)

            active = projects_dir / "active-session.jsonl"
            active.write_text(
                json.dumps({"cwd": project_root, "sessionId": "active-session"}) + "\n",
                encoding="utf-8",
            )
            time.sleep(0.01)
            newer = projects_dir / "newer-session.jsonl"
            newer.write_text(
                json.dumps({"cwd": project_root, "sessionId": "newer-session"}) + "\n",
                encoding="utf-8",
            )

            project_state.set_active_session_id(open_id, project, "active-session")

            with patch.dict(os.environ, {"HOME": home}, clear=False):
                self.assertEqual(
                    runner._resume_session_id_for_project(open_id, project, project_root),
                    "active-session",
                )


if __name__ == "__main__":
    unittest.main()
