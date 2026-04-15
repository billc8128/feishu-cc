"""飞书事件接收、解密、去重、基础准入过滤。

设计要点:
- 用 lark-oapi 内置的事件验证/解密(URL 验证、AES 解密、签名校验)
- event_id 用进程内 LRU 去重(飞书重试窗口几分钟,无需持久化)
- 群聊默认全部忽略(首版自用)
- 细粒度访问控制在 app.py 里处理,这样未开通用户也能收到 /apply 引导
"""
from __future__ import annotations

import json
import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional

import lark_oapi as lark

from config import settings

logger = logging.getLogger(__name__)


# ---------- event_id 去重(LRU,容量 1024) ----------

class _LRUSet:
    def __init__(self, capacity: int = 1024) -> None:
        self.capacity = capacity
        self._d: "OrderedDict[str, None]" = OrderedDict()

    def add_if_absent(self, key: str) -> bool:
        """加入 key 若已存在返回 False(说明是重复事件)。"""
        if key in self._d:
            self._d.move_to_end(key)
            return False
        self._d[key] = None
        if len(self._d) > self.capacity:
            self._d.popitem(last=False)
        return True


_seen_events = _LRUSet(capacity=2048)


# ---------- 解析后的事件结构 ----------

@dataclass
class IncomingAttachment:
    kind: str
    file_key: str
    message_resource_type: str
    file_name: str | None = None
    file_type: str | None = None


@dataclass
class ParsedMessageEvent:
    """业务层关心的字段都拍平在这里,不再依赖 lark 的嵌套类型。"""

    event_id: str
    sender_open_id: str
    chat_id: str
    chat_type: str  # "p2p" 或 "group"
    message_id: str
    text: str  # 已剥离 @机器人 前缀的纯文本
    attachments: list[IncomingAttachment]


@dataclass
class ParsedCardActionEvent:
    event_id: str
    operator_open_id: str
    open_message_id: str
    action_tag: str
    action_value: dict


# ---------- URL 验证(飞书后台首次配 webhook 时的握手) ----------

def is_url_verification(body: dict) -> bool:
    return body.get("type") == "url_verification"


def url_verification_response(body: dict) -> dict:
    return {"challenge": body.get("challenge", "")}


# ---------- 事件解密 ----------

def decrypt_event(raw_body: bytes) -> dict:
    """如果配置了 encrypt_key,飞书发来的是 {"encrypt": "..."} 的密文。

    返回解密后的明文 dict。未配置加密则原样 json 解析。
    """
    body = json.loads(raw_body.decode("utf-8"))
    if "encrypt" in body and settings.feishu_encrypt_key:
        cipher = lark.AESCipher(settings.feishu_encrypt_key)
        plaintext = cipher.decrypt_string(body["encrypt"])
        return json.loads(plaintext)
    return body


# ---------- 事件解析 ----------

def parse_message_event(body: dict) -> Optional[ParsedMessageEvent]:
    """从飞书原始事件中提取出业务关心的字段。

    只处理 im.message.receive_v1 类型的事件。其他事件返回 None。
    """
    header = body.get("header", {})
    event_type = header.get("event_type")
    if event_type != "im.message.receive_v1":
        return None

    event_id = header.get("event_id", "")
    event = body.get("event", {})
    sender = event.get("sender", {})
    sender_id = sender.get("sender_id", {})
    open_id = sender_id.get("open_id", "")

    message = event.get("message", {})
    chat_id = message.get("chat_id", "")
    chat_type = message.get("chat_type", "")
    message_id = message.get("message_id", "")
    msg_type = message.get("message_type", "")

    text = ""
    attachments: list[IncomingAttachment] = []
    if msg_type == "text":
        text = _parse_text_content(message)
    elif msg_type == "post":
        text, attachments = _parse_post_content(message)
        if not text and not attachments:
            return None
    elif msg_type == "image":
        attachment = _parse_image_attachment(message)
        if not attachment:
            return None
        attachments.append(attachment)
    elif msg_type == "file":
        attachment = _parse_file_attachment(message)
        if not attachment:
            return None
        attachments.append(attachment)
    else:
        return None

    return ParsedMessageEvent(
        event_id=event_id,
        sender_open_id=open_id,
        chat_id=chat_id,
        chat_type=chat_type,
        message_id=message_id,
        text=text,
        attachments=attachments,
    )


def parse_card_action_event(body: dict) -> Optional[ParsedCardActionEvent]:
    """解析飞书交互卡片按钮回调。"""
    header = body.get("header") or {}
    if header.get("event_type") not in {"p2.card.action.trigger", "card.action.trigger"}:
        return None

    event = body.get("event") or {}
    operator = event.get("operator") or {}
    context = event.get("context") or {}
    action = event.get("action") or {}
    action_value = action.get("value")
    if not isinstance(action_value, dict):
        action_value = {}

    return ParsedCardActionEvent(
        event_id=header.get("event_id", ""),
        operator_open_id=operator.get("open_id", ""),
        open_message_id=context.get("open_message_id", ""),
        action_tag=str(action.get("tag") or ""),
        action_value=action_value,
    )


def _strip_at_bot(text: str) -> str:
    """去掉群消息里 @机器人 的前缀(@_user_1 之类)。"""
    parts = text.split()
    parts = [p for p in parts if not p.startswith("@_user_")]
    return " ".join(parts).strip()


def _parse_text_content(message: dict) -> str:
    try:
        content_obj = json.loads(message.get("content", "{}"))
    except json.JSONDecodeError:
        return ""
    return _strip_at_bot(content_obj.get("text", "").strip())


def _parse_post_content(message: dict) -> tuple[str, list[IncomingAttachment]]:
    try:
        content_obj = json.loads(message.get("content", "{}"))
    except json.JSONDecodeError:
        return "", []

    post = _unwrap_post_content(content_obj)
    if not isinstance(post, dict):
        return "", []

    text_parts: list[str] = []
    attachments: list[IncomingAttachment] = []

    title = str(post.get("title", "")).strip()
    if title:
        text_parts.append(title)

    rows = post.get("content", [])
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, list):
                _collect_post_row(row, text_parts, attachments)

    return _strip_at_bot(" ".join(text_parts).strip()), attachments


def _unwrap_post_content(content_obj: object) -> object:
    if isinstance(content_obj, dict) and "content" in content_obj:
        return content_obj

    if isinstance(content_obj, dict):
        for value in content_obj.values():
            if isinstance(value, dict) and "content" in value:
                return value

    return content_obj


def _collect_post_row(
    row: list[object],
    text_parts: list[str],
    attachments: list[IncomingAttachment],
) -> None:
    for item in row:
        if not isinstance(item, dict):
            continue

        tag = item.get("tag")
        if tag == "text":
            text = str(item.get("text", "")).strip()
            if text:
                text_parts.append(text)
            continue

        if tag == "a":
            text = str(item.get("text") or item.get("href") or "").strip()
            if text:
                text_parts.append(text)
            continue

        if tag == "img":
            image_key = item.get("image_key")
            if image_key:
                attachments.append(
                    IncomingAttachment(
                        kind="image",
                        file_key=str(image_key),
                        message_resource_type="image",
                    )
                )
            continue

        nested = item.get("elements")
        if isinstance(nested, list):
            _collect_post_row(nested, text_parts, attachments)


def _parse_image_attachment(message: dict) -> IncomingAttachment | None:
    try:
        content_obj = json.loads(message.get("content", "{}"))
    except json.JSONDecodeError:
        return None

    image_key = content_obj.get("image_key")
    if not image_key:
        return None
    return IncomingAttachment(
        kind="image",
        file_key=image_key,
        message_resource_type="image",
        file_name=content_obj.get("file_name"),
        file_type=content_obj.get("file_type"),
    )


def _parse_file_attachment(message: dict) -> IncomingAttachment | None:
    try:
        content_obj = json.loads(message.get("content", "{}"))
    except json.JSONDecodeError:
        return None

    file_key = content_obj.get("file_key")
    if not file_key:
        return None

    file_name = content_obj.get("file_name")
    file_type = content_obj.get("file_type")
    return IncomingAttachment(
        kind=_classify_file_kind(file_name, file_type),
        file_key=file_key,
        message_resource_type="file",
        file_name=file_name,
        file_type=file_type,
    )


def _classify_file_kind(file_name: str | None, file_type: str | None) -> str:
    if file_type and file_type.lower() in {
        "mp4",
        "mov",
        "avi",
        "mkv",
        "webm",
        "mpeg",
        "mpg",
        "wmv",
        "m4v",
    }:
        return "video"

    if file_name:
        suffix = file_name.rsplit(".", 1)
        if len(suffix) == 2 and suffix[1].lower() in {
            "mp4",
            "mov",
            "avi",
            "mkv",
            "webm",
            "mpeg",
            "mpg",
            "wmv",
            "m4v",
        }:
            return "video"

    return "file"


# ---------- 准入校验 ----------

def is_duplicate(event_id: str) -> bool:
    if not event_id:
        return False
    return not _seen_events.add_if_absent(event_id)


def is_allowed(parsed: ParsedMessageEvent) -> bool:
    """只处理私聊事件;访问审批在分发层判断。"""
    # 首版禁用群聊
    if parsed.chat_type != "p2p":
        logger.info("ignoring non-p2p chat: chat_type=%s", parsed.chat_type)
        return False

    return True
