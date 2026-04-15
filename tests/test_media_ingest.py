import asyncio
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("ANTHROPIC_AUTH_TOKEN", "test-token")
os.environ.setdefault("FEISHU_APP_ID", "test-app-id")
os.environ.setdefault("FEISHU_APP_SECRET", "test-app-secret")

from feishu.events import IncomingAttachment
from media.ingest import ingest_attachments, sanitize_filename


class MediaIngestTests(unittest.TestCase):
    def test_sanitize_filename_preserves_extension(self) -> None:
        self.assertEqual(
            sanitize_filename("../../bad name!!.mp4"),
            "bad-name.mp4",
        )

    def test_ingest_attachments_saves_into_project_inbox(self) -> None:
        class StubFeishuClient:
            async def download_message_resource(self, **kwargs):
                destination = kwargs["destination"]
                destination.write_bytes(b"video")
                return destination

        async def run_test() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                project_root = Path(tmp)
                attachments = [
                    IncomingAttachment(
                        kind="video",
                        file_key="file_key",
                        message_resource_type="file",
                        file_name="demo.mp4",
                        file_type="mp4",
                    )
                ]
                stored = await ingest_attachments(
                    feishu=StubFeishuClient(),
                    project_root=project_root,
                    message_id="om_1",
                    attachments=attachments,
                )
                self.assertEqual(stored[0].local_path.parent.name, "inbox")
                self.assertTrue(stored[0].local_path.exists())

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
