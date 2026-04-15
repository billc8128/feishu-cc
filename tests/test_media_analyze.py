import asyncio
import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("ANTHROPIC_AUTH_TOKEN", "test-token")
os.environ.setdefault("FEISHU_APP_ID", "test-app-id")
os.environ.setdefault("FEISHU_APP_SECRET", "test-app-secret")

from media.analyze import MediaAnalyzer
from media.ingest import MediaAttachment


class MediaAnalyzeTests(unittest.TestCase):
    def test_image_analysis_normalizes_vlm_response(self) -> None:
        async def run_test() -> None:
            analyzer = MediaAnalyzer()
            with patch.object(
                analyzer,
                "_call_glm_vision",
                new=AsyncMock(
                    return_value={
                        "summary": "像素风平台跳跃视频截图",
                        "ocr_text": "",
                        "visual_elements": ["角色", "平台"],
                        "actions_or_mechanics": ["跳跃"],
                        "suggested_intent": "分析游戏机制",
                    }
                ),
            ):
                result = await analyzer.analyze(
                    MediaAttachment(
                        kind="image",
                        original_name="frame.png",
                        local_path=Path("/tmp/frame.png"),
                    ),
                    user_text="帮我拆解玩法",
                )
                self.assertEqual(result.kind, "image")
                self.assertIn("平台", result.visual_elements)

        asyncio.run(run_test())

    def test_video_analysis_falls_back_to_frames(self) -> None:
        async def run_test() -> None:
            analyzer = MediaAnalyzer()
            video = MediaAttachment(
                kind="video",
                original_name="clip.mp4",
                local_path=Path("/tmp/clip.mp4"),
            )
            with patch.object(
                analyzer,
                "_call_glm_vision",
                new=AsyncMock(
                    side_effect=[
                        RuntimeError("video failed"),
                        {
                            "summary": "抽帧后的总结",
                            "ocr_text": "",
                            "visual_elements": ["角色"],
                            "actions_or_mechanics": ["移动"],
                            "suggested_intent": "继续分析",
                        },
                    ]
                ),
            ), patch.object(
                analyzer,
                "_extract_video_frames",
                return_value=[Path("/tmp/frame-001.png")],
            ):
                result = await analyzer.analyze(video, user_text="")
                self.assertTrue(result.fallback_used)

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
