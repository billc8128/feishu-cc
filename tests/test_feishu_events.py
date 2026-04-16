import os
import unittest

os.environ.setdefault("ANTHROPIC_AUTH_TOKEN", "test-token")
os.environ.setdefault("FEISHU_APP_ID", "test-app-id")
os.environ.setdefault("FEISHU_APP_SECRET", "test-app-secret")
os.environ.setdefault("DATA_DIR", "/tmp/feishu-cc-test-data")
os.environ.setdefault("BROWSER_SERVICE_BASE_URL", "https://browser.example.com")
os.environ.setdefault("BROWSER_SERVICE_TOKEN", "browser-token")

from config import settings
from feishu.events import (
    ParsedMessageEvent,
    extract_verification_token,
    is_allowed,
    parse_card_action_event,
    parse_message_event,
)


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

    def test_parses_post_message_with_text_and_image(self) -> None:
        body = {
            "header": {"event_type": "im.message.receive_v1", "event_id": "evt-4"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_123"}},
                "message": {
                    "chat_id": "oc_1",
                    "chat_type": "p2p",
                    "message_id": "om_4",
                    "message_type": "post",
                    "content": (
                        "{"
                        "\"title\":\"\","
                        "\"content\":["
                        "[{\"tag\":\"text\",\"text\":\"这是图片说明：\"},"
                        "{\"tag\":\"a\",\"text\":\"文档\",\"href\":\"https://example.com\"}],"
                        "[{\"tag\":\"img\",\"image_key\":\"img_key\"}],"
                        "[{\"tag\":\"text\",\"text\":\"请帮我看下\"}]"
                        "]"
                        "}"
                    ),
                },
            },
        }

        parsed = parse_message_event(body)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.text, "这是图片说明： 文档 请帮我看下")
        self.assertEqual(len(parsed.attachments), 1)
        self.assertEqual(parsed.attachments[0].kind, "image")
        self.assertEqual(parsed.attachments[0].file_key, "img_key")

    def test_parses_browser_approval_card_action_event(self) -> None:
        body = {
            "header": {"event_type": "p2.card.action.trigger", "event_id": "evt-card-1"},
            "event": {
                "operator": {"open_id": "ou_123"},
                "context": {"open_message_id": "om_123"},
                "action": {
                    "tag": "button",
                    "value": {"kind": "browser_approval", "decision": "yes"},
                },
            },
        }

        parsed = parse_card_action_event(body)

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.event_id, "evt-card-1")
        self.assertEqual(parsed.operator_open_id, "ou_123")
        self.assertEqual(parsed.open_message_id, "om_123")
        self.assertEqual(parsed.action_value["kind"], "browser_approval")
        self.assertEqual(parsed.action_value["decision"], "yes")

    def test_parses_legacy_browser_approval_card_action_payload(self) -> None:
        body = {
            "open_id": "ou_legacy",
            "open_message_id": "om_legacy",
            "token": "verification-token",
            "action": {
                "tag": "button",
                "value": {"kind": "browser_approval", "decision": "no"},
            },
        }

        parsed = parse_card_action_event(body)

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.event_id, "")
        self.assertEqual(parsed.operator_open_id, "ou_legacy")
        self.assertEqual(parsed.open_message_id, "om_legacy")
        self.assertEqual(parsed.action_tag, "button")
        self.assertEqual(parsed.action_value["decision"], "no")

    def test_extracts_verification_token_from_legacy_card_payload(self) -> None:
        body = {
            "open_id": "ou_legacy",
            "token": "verification-token",
            "action": {"tag": "button", "value": {"kind": "browser_approval", "decision": "yes"}},
        }

        token = extract_verification_token(body)

        self.assertEqual(token, "verification-token")

    def test_allows_p2p_messages_without_static_whitelist(self) -> None:
        settings.feishu_allowed_open_ids = ""
        parsed = ParsedMessageEvent(
            event_id="evt-5",
            sender_open_id="ou_unknown",
            chat_id="oc_1",
            chat_type="p2p",
            message_id="om_5",
            text="/apply",
            attachments=[],
        )

        self.assertTrue(is_allowed(parsed))

    def test_rejects_non_p2p_messages(self) -> None:
        parsed = ParsedMessageEvent(
            event_id="evt-6",
            sender_open_id="ou_unknown",
            chat_id="oc_2",
            chat_type="group",
            message_id="om_6",
            text="/apply",
            attachments=[],
        )

        self.assertFalse(is_allowed(parsed))


if __name__ == "__main__":
    unittest.main()
