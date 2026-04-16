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
    def test_send_markdown_builds_patchable_card(self) -> None:
        async def run_test() -> None:
            client = FeishuClient()
            client._create_message = AsyncMock(return_value="om_card")  # type: ignore[method-assign]

            message_id = await client.send_markdown(
                "ou_123",
                "任务运行中",
                title="运行状态",
            )

            self.assertEqual(message_id, "om_card")
            _, kwargs = client._create_message.await_args
            self.assertEqual(kwargs["msg_type"], "interactive")
            card = json.loads(kwargs["content"])
            self.assertTrue(card["config"]["update_multi"])
            self.assertEqual(card["header"]["title"]["content"], "运行状态")
            self.assertEqual(card["elements"][0]["content"], "任务运行中")

        asyncio.run(run_test())

    def test_update_markdown_reuses_markdown_card_payload(self) -> None:
        async def run_test() -> None:
            client = FeishuClient()
            client._patch_message_content = AsyncMock(return_value=True)  # type: ignore[method-assign]

            ok = await client.update_markdown(
                "om_card",
                "继续运行",
                title="运行状态",
            )

            self.assertTrue(ok)
            args = client._patch_message_content.await_args.args
            self.assertEqual(args[0], "om_card")
            card = json.loads(args[1])
            self.assertTrue(card["config"]["update_multi"])
            self.assertEqual(card["header"]["title"]["content"], "运行状态")
            self.assertEqual(card["elements"][0]["content"], "继续运行")

        asyncio.run(run_test())

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

    def test_send_browser_approval_card_includes_scheduled_trust_note_when_provided(self) -> None:
        async def run_test() -> None:
            client = FeishuClient()
            client._create_message = AsyncMock(return_value="om_card")  # type: ignore[method-assign]

            await client.send_browser_approval_card(
                "ou_123",
                reason="需要登录 Reddit",
                trust_note="允许后，此定时任务后续将自动使用浏览器，不再重复询问。",
            )

            _, kwargs = client._create_message.await_args
            card = json.loads(kwargs["content"])
            self.assertIn("允许后，此定时任务后续将自动使用浏览器，不再重复询问。", card["elements"][0]["content"])
            self.assertIn("需要登录 Reddit", card["elements"][0]["content"])

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

    def test_get_user_display_name_returns_name_for_open_id(self) -> None:
        async def run_test() -> None:
            client = FeishuClient()
            fake_response = Mock()
            fake_response.success.return_value = True
            fake_response.data = type(
                "StubData",
                (),
                {
                    "user": type(
                        "StubUser",
                        (),
                        {"name": "朱政怡"},
                    )()
                },
            )()
            fake_api = Mock()
            fake_api.aget = AsyncMock(return_value=fake_response)
            client._client = type(
                "StubClient",
                (),
                {
                    "contact": type(
                        "StubContact",
                        (),
                        {"v3": type("StubV3", (), {"user": fake_api})()},
                    )()
                },
            )()

            name = await client.get_user_display_name("ou_123")

            self.assertEqual(name, "朱政怡")

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
