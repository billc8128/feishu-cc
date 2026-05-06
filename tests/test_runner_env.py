import importlib
import os
import sys
import types
import unittest
from types import SimpleNamespace
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
    fake_sdk.create_sdk_mcp_server = lambda *args, **kwargs: {}
    fake_sdk.tool = lambda *args, **kwargs: (lambda fn: fn)

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
config = importlib.import_module("config")


class RunnerEnvTests(unittest.TestCase):
    def test_settings_default_agent_backend_uses_zai_claude_code_endpoint(self) -> None:
        settings = config.Settings(
            anthropic_auth_token="token",
            feishu_app_id="test-app-id",
            feishu_app_secret="test-app-secret",
        )

        self.assertEqual(
            settings.anthropic_base_url,
            "https://api.z.ai/api/anthropic",
        )
        self.assertEqual(settings.anthropic_model, "")
        self.assertEqual(settings.anthropic_default_opus_model, "glm-5.1")
        self.assertEqual(settings.anthropic_default_sonnet_model, "glm-5.1")
        self.assertEqual(settings.anthropic_default_haiku_model, "glm-4.5-air")

    def test_inject_env_sets_anthropic_model_when_configured(self) -> None:
        fake_settings = SimpleNamespace(
            anthropic_auth_token="token",
            anthropic_base_url="https://api.z.ai/api/anthropic",
            anthropic_model="",
            anthropic_default_opus_model="glm-5.1",
            anthropic_default_sonnet_model="glm-5.1",
            anthropic_default_haiku_model="glm-4.5-air",
            api_timeout_ms="3000000",
            claude_code_disable_nonessential_traffic="1",
        )

        with patch.object(runner, "settings", fake_settings), patch.dict(
            os.environ, {}, clear=True
        ):
            runner._inject_anthropic_env()

            self.assertEqual(
                os.environ["ANTHROPIC_BASE_URL"],
                "https://api.z.ai/api/anthropic",
            )
            self.assertNotIn("ANTHROPIC_MODEL", os.environ)
            self.assertEqual(os.environ["ANTHROPIC_DEFAULT_OPUS_MODEL"], "glm-5.1")
            self.assertEqual(os.environ["ANTHROPIC_DEFAULT_SONNET_MODEL"], "glm-5.1")
            self.assertEqual(
                os.environ["ANTHROPIC_DEFAULT_HAIKU_MODEL"],
                "glm-4.5-air",
            )


if __name__ == "__main__":
    unittest.main()
