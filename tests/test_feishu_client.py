import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

os.environ.setdefault("ANTHROPIC_AUTH_TOKEN", "test-token")
os.environ.setdefault("FEISHU_APP_ID", "test-app-id")
os.environ.setdefault("FEISHU_APP_SECRET", "test-app-secret")
os.environ.setdefault("DATA_DIR", "/tmp/feishu-cc-test-data")
os.environ.setdefault("BROWSER_SERVICE_BASE_URL", "https://browser.example.com")
os.environ.setdefault("BROWSER_SERVICE_TOKEN", "browser-token")

if "feishu.client" in sys.modules and not hasattr(sys.modules["feishu.client"], "FeishuClient"):
    del sys.modules["feishu.client"]

from feishu.client import FeishuClient


class FeishuDownloadTests(unittest.TestCase):
    def test_send_browser_approval_card_builds_buttons_and_fallback_text(self) -> None:
        async def run_test() -> None:
            client = FeishuClient()
            client._create_message = AsyncMock(return_value="om_card")  # type: ignore[method-assign]

            message_id = await client.send_browser_approval_card(
                "ou_123",
                reason="需要登录 Reddit",
            )

            self.assertEqual(message_id, "om_card")
            _, kwargs = client._create_message.await_args
            self.assertEqual(kwargs["receive_id_type"], "open_id")
            self.assertEqual(kwargs["receive_id"], "ou_123")
            self.assertEqual(kwargs["msg_type"], "interactive")
            card = json.loads(kwargs["content"])
            self.assertIn("需要登录 Reddit", card["elements"][0]["content"])
            self.assertIn("/browser yes", card["elements"][0]["content"])
            actions = card["elements"][1]["actions"]
            self.assertEqual(actions[0]["value"]["kind"], "browser_approval")
            self.assertEqual(actions[0]["value"]["decision"], "yes")
            self.assertEqual(actions[1]["value"]["decision"], "no")

        asyncio.run(run_test())

    def test_download_message_resource_writes_bytes(self) -> None:
        async def run_test() -> None:
            client = FeishuClient()
            fake_response = Mock()
            fake_response.success.return_value = True
            fake_response.file.read.return_value = b"image-bytes"
            fake_api = Mock()
            fake_api.aget = AsyncMock(return_value=fake_response)
            client._client = type(
                "StubClient",
                (),
                {
                    "im": type(
                        "StubIM",
                        (),
                        {"v1": type("StubV1", (), {"message_resource": fake_api})()},
                    )()
                },
            )()

            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "saved.png"
                saved = await client.download_message_resource(
                    message_id="om_1",
                    file_key="img_key",
                    resource_type="image",
                    destination=out,
                )
                self.assertEqual(saved, out)
                self.assertEqual(out.read_bytes(), b"image-bytes")

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
