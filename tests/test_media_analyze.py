import asyncio
import base64
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("ANTHROPIC_AUTH_TOKEN", "test-token")
os.environ.setdefault("FEISHU_APP_ID", "test-app-id")
os.environ.setdefault("FEISHU_APP_SECRET", "test-app-secret")

from media.analyze import MediaAnalyzer, _path_to_data_url
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

    def test_path_to_data_url_prefers_attachment_mime_type(self) -> None:
        png_bytes = (
            b"\x89PNG\r\n\x1a\n"
            b"\x00\x00\x00\rIHDR"
            b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
            b"\x90wS\xde"
            b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfeA\r\x98\xdb"
            b"\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        path = Path("/tmp/image-without-extension")

        with patch.object(Path, "read_bytes", return_value=png_bytes):
            data_url = _path_to_data_url(path, mime_type="image/png")

        self.assertTrue(data_url.startswith("data:image/png;base64,"))
        self.assertIn(base64.b64encode(png_bytes).decode("ascii"), data_url)

    def test_vision_call_prefers_dedicated_api_key_when_configured(self) -> None:
        async def run_test() -> None:
            class FakeResponse:
                def raise_for_status(self) -> None:
                    pass

                def json(self) -> dict:
                    return {
                        "choices": [
                            {
                                "message": {
                                    "content": '{"summary": "ok"}',
                                }
                            }
                        ]
                    }

            class FakeAsyncClient:
                def __init__(self, *args, **kwargs) -> None:
                    self.headers = None

                async def __aenter__(self):
                    return self

                async def __aexit__(self, exc_type, exc, tb) -> None:
                    pass

                async def post(self, url, *, headers, json):
                    self.headers = headers
                    return FakeResponse()

            fake_client = FakeAsyncClient()
            fake_settings = SimpleNamespace(
                anthropic_auth_token="coding-token",
                glm_vision_api_key="vision-token",
                glm_vision_model="doubao-vision",
                glm_vision_base_url="https://ark.cn-beijing.volces.com/api/v3/chat/completions",
                api_timeout_ms="3000000",
            )

            with patch("media.analyze.settings", fake_settings), patch(
                "media.analyze.httpx.AsyncClient",
                return_value=fake_client,
            ), patch.object(Path, "read_bytes", return_value=b"image"):
                await MediaAnalyzer()._call_glm_vision(
                    media_kind="image",
                    media_items=[(Path("/tmp/demo.png"), "image/png")],
                    user_text="分析图片",
                )

            self.assertEqual(
                fake_client.headers["Authorization"],
                "Bearer vision-token",
            )

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
