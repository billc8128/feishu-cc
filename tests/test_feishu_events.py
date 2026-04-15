import os
import unittest

os.environ.setdefault("ANTHROPIC_AUTH_TOKEN", "test-token")
os.environ.setdefault("FEISHU_APP_ID", "test-app-id")
os.environ.setdefault("FEISHU_APP_SECRET", "test-app-secret")

from feishu.events import parse_message_event


class ParseMessageEventTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
