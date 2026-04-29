import os
import unittest
from pathlib import Path

os.environ.setdefault("ANTHROPIC_AUTH_TOKEN", "test-token")
os.environ.setdefault("FEISHU_APP_ID", "test-app-id")
os.environ.setdefault("FEISHU_APP_SECRET", "test-app-secret")

from media.analyze import MediaAnalysis
from media.ingest import MediaAttachment
from media.prompting import build_media_turn_prompt


class MediaPromptingTests(unittest.TestCase):
    def test_build_media_turn_prompt_handles_pure_media_message(self) -> None:
        prompt = build_media_turn_prompt(
            text="",
            attachments=[
                MediaAttachment(
                    kind="image",
                    original_name="demo.png",
                    local_path=Path("/tmp/demo.png"),
                )
            ],
            analyses=[
                MediaAnalysis(
                    kind="image",
                    summary="界面截图，展示任务列表。",
                    ocr_text="Task list",
                    visual_elements=["列表", "按钮"],
                    actions_or_mechanics=["浏览任务"],
                    suggested_intent="让 agent 分析截图内容",
                    fallback_used=False,
                )
            ],
        )
        self.assertIn("用户发送了一条飞书消息", prompt)
        self.assertIn("/tmp/demo.png", prompt)
        self.assertIn("界面截图", prompt)

    def test_failed_media_analysis_warns_agent_not_to_read_media_directly(self) -> None:
        prompt = build_media_turn_prompt(
            text="帮我看下这张图",
            attachments=[
                MediaAttachment(
                    kind="image",
                    original_name="broken.png",
                    local_path=Path("/tmp/broken.png"),
                )
            ],
            analyses=[None],
        )

        self.assertIn("媒体分析失败", prompt)
        self.assertIn("不要直接读取图片/视频", prompt)
        self.assertNotIn("请按本地文件继续处理", prompt)


if __name__ == "__main__":
    unittest.main()
