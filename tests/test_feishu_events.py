import os
import unittest

os.environ.setdefault("ANTHROPIC_AUTH_TOKEN", "test-token")
os.environ.setdefault("FEISHU_APP_ID", "test-app-id")
os.environ.setdefault("FEISHU_APP_SECRET", "test-app-secret")

from config import settings
from feishu.events import ParsedMessageEvent, is_allowed, parse_message_event


class ParseMessageEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self._original_allowed_ids = settings.feishu_allowed_open_ids

    def tearDown(self) -> None:
        settings.feishu_allowed_open_ids = self._original_allowed_ids

    def test_parses_text_message_without_attachments(self) -> None:
        body = {
            "header": {"event_type": "im.message.receive_v1", "event_id": "evt-1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_123"}},
                "message": {
                    "chat_id": "oc_1",
                    "chat_type": "p2p",
                    "message_id": "om_1",
                    "message_type": "text",
                    "content": "{\"text\": \"你好\"}",
                },
            },
        }
        parsed = parse_message_event(body)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.text, "你好")
        self.assertEqual(parsed.attachments, [])

    def test_parses_image_message_as_attachment(self) -> None:
        body = {
            "header": {"event_type": "im.message.receive_v1", "event_id": "evt-2"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_123"}},
                "message": {
                    "chat_id": "oc_1",
                    "chat_type": "p2p",
                    "message_id": "om_2",
                    "message_type": "image",
                    "content": "{\"image_key\": \"img_key\"}",
                },
            },
        }
        parsed = parse_message_event(body)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.text, "")
        self.assertEqual(parsed.attachments[0].kind, "image")
        self.assertEqual(parsed.attachments[0].file_key, "img_key")

    def test_parses_video_file_message_as_video_attachment(self) -> None:
        body = {
            "header": {"event_type": "im.message.receive_v1", "event_id": "evt-3"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_123"}},
                "message": {
                    "chat_id": "oc_1",
                    "chat_type": "p2p",
                    "message_id": "om_3",
                    "message_type": "file",
                    "content": "{\"file_key\": \"file_key\", \"file_name\": \"clip.mp4\"}",
                },
            },
        }
        parsed = parse_message_event(body)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.attachments[0].kind, "video")

    def test_allows_p2p_messages_without_static_whitelist(self) -> None:
        settings.feishu_allowed_open_ids = ""
        parsed = ParsedMessageEvent(
            event_id="evt-4",
            sender_open_id="ou_unknown",
            chat_id="oc_1",
            chat_type="p2p",
            message_id="om_4",
            text="/apply",
            attachments=[],
        )

        self.assertTrue(is_allowed(parsed))

    def test_rejects_non_p2p_messages(self) -> None:
        parsed = ParsedMessageEvent(
            event_id="evt-5",
            sender_open_id="ou_unknown",
            chat_id="oc_2",
            chat_type="group",
            message_id="om_5",
            text="/apply",
            attachments=[],
        )

        self.assertFalse(is_allowed(parsed))


if __name__ == "__main__":
    unittest.main()
