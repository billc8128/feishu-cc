import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

os.environ.setdefault("ANTHROPIC_AUTH_TOKEN", "test-token")
os.environ.setdefault("FEISHU_APP_ID", "test-app-id")
os.environ.setdefault("FEISHU_APP_SECRET", "test-app-secret")

from feishu.client import FeishuClient


class FeishuDownloadTests(unittest.TestCase):
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
