import asyncio
import importlib
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("ANTHROPIC_AUTH_TOKEN", "test-token")
os.environ.setdefault("FEISHU_APP_ID", "test-app-id")
os.environ.setdefault("FEISHU_APP_SECRET", "test-app-secret")
os.environ.setdefault("DATA_DIR", "/tmp/feishu-cc-test-data")


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

    fake_feishu_client_module = types.ModuleType("feishu.client")
    fake_feishu_client_module.feishu_client = type(
        "FakeFeishuClient",
        (),
        {
            "send_text": AsyncMock(),
            "send_markdown": AsyncMock(return_value=True),
        },
    )()

    sys.modules.setdefault("claude_agent_sdk", fake_sdk)
    sys.modules.setdefault("agent.hooks", fake_hooks)
    sys.modules.setdefault("agent.tools_schedule", fake_schedule)
    sys.modules.setdefault("agent.tools_deliver", fake_deliver)
    sys.modules.setdefault("feishu.client", fake_feishu_client_module)


_install_test_stubs()
runner = importlib.import_module("agent.runner")
app_module = importlib.import_module("app")

from feishu.events import IncomingAttachment, ParsedMessageEvent
from media.analyze import MediaAnalysis
from media.ingest import MediaAttachment


class MediaFlowTests(unittest.TestCase):
    def test_handle_incoming_message_builds_prompt_from_media(self) -> None:
        async def run_test() -> None:
            pooled = runner._PooledClient(client=object(), project="scratch")
            attachment = IncomingAttachment(
                kind="video",
                file_key="file_key",
                message_resource_type="file",
                file_name="clip.mp4",
                file_type="mp4",
            )
            stored_attachment = MediaAttachment(
                kind="video",
                original_name="clip.mp4",
                local_path=Path("/tmp/clip.mp4"),
            )
            analysis = MediaAnalysis(
                kind="video",
                summary="媒体分析摘要：角色在平台间移动。",
                ocr_text="",
                visual_elements=["角色"],
                actions_or_mechanics=["移动"],
                suggested_intent="分析视频玩法",
                fallback_used=False,
            )

            with tempfile.TemporaryDirectory() as tmp:
                with patch.object(
                    runner.project_state,
                    "get_current_project",
                    return_value="scratch",
                ), patch.object(
                    runner.project_manager,
                    "ensure_project_root",
                    return_value=Path(tmp),
                ), patch.object(
                    runner,
                    "_get_or_create_client",
                    new=AsyncMock(return_value=pooled),
                ), patch.object(
                    runner,
                    "ingest_attachments",
                    new=AsyncMock(return_value=[stored_attachment]),
                ), patch.object(
                    runner,
                    "_run_query",
                    new=AsyncMock(),
                ) as run_query, patch.object(
                    runner.MediaAnalyzer,
                    "analyze",
                    new=AsyncMock(return_value=analysis),
                ):
                    await runner.handle_incoming_message(
                        "ou_123",
                        text="帮我分析这个视频",
                        message_id="om_1",
                        attachments=[attachment],
                    )

            sent_prompt = run_query.await_args.args[3]
            self.assertIn("媒体分析摘要", sent_prompt)
            self.assertIn("/tmp/clip.mp4", sent_prompt)

        asyncio.run(run_test())

    def test_dispatch_routes_pure_media_messages_to_media_handler(self) -> None:
        async def run_test() -> None:
            parsed = ParsedMessageEvent(
                event_id="evt-1",
                sender_open_id="ou_123",
                chat_id="oc_1",
                chat_type="p2p",
                message_id="om_1",
                text="",
                attachments=[
                    IncomingAttachment(
                        kind="image",
                        file_key="img_key",
                        message_resource_type="image",
                    )
                ],
            )

            with patch.object(
                runner,
                "handle_incoming_message",
                new=AsyncMock(),
            ) as handle_incoming_message, patch.object(
                runner,
                "handle_user_message",
                new=AsyncMock(),
            ) as handle_user_message:
                await app_module._dispatch(parsed)
                handle_incoming_message.assert_awaited_once()
                handle_user_message.assert_not_called()

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
