"""飞书 API 封装层。只做协议适配,不含业务逻辑。

基于 lark-oapi(飞书官方 Python SDK)。所有 token 刷新、签名校验、加密解密
都由官方 SDK 处理,我们只暴露最少的、业务层需要的几个动作。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateFileRequest,
    CreateFileRequestBody,
    CreateImageRequest,
    CreateImageRequestBody,
    GetMessageResourceRequest,
    CreateMessageRequest,
    CreateMessageRequestBody,
    CreateMessageResponse,
    PatchMessageRequest,
    PatchMessageRequestBody,
)
from lark_oapi.api.contact.v3 import GetUserRequest

from config import settings

logger = logging.getLogger(__name__)


class FeishuClient:
    """飞书客户端的最小封装。

    懒加载:lark client 在第一次真正用到时才构造。这样模块 import 时
    占位符 app_id/app_secret 不会让 lark builder 校验失败,容器可以
    顺利起来等待真正的飞书凭证被填入。
    """

    def __init__(self) -> None:
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = (
                lark.Client.builder()
                .app_id(settings.feishu_app_id)
                .app_secret(settings.feishu_app_secret)
                .log_level(lark.LogLevel.WARNING)
                .build()
            )
        return self._client

    # ---------- 发送消息 ----------

    async def send_text(self, open_id: str, text: str) -> Optional[str]:
        """发文本消息给某个用户。返回新消息的 message_id,失败返回 None。

        消息长度过长会被飞书拒绝,我们这里截断到 安全长度内。
        """
        # 飞书单条文本消息的硬上限是 30000 字符,留点余地
        if len(text) > 28000:
            text = text[:28000] + "\n\n…(消息过长已截断)"

        return await self._create_message(
            receive_id_type="open_id",
            receive_id=open_id,
            msg_type="text",
            content=json.dumps({"text": text}, ensure_ascii=False),
        )

    async def send_markdown(
        self,
        open_id: str,
        md: str,
        title: Optional[str] = None,
    ) -> Optional[str]:
        """发交互式卡片,渲染 Markdown 文本。

        - title 不给就不渲染 header(更适合日常对话的轻量卡片)
        - 飞书 markdown 元素原生支持代码块(```lang + 代码),飞书 7.6+
          客户端会自动给代码块加复制按钮,不需要我们额外做 action 元素
        - 飞书卡片单条总 JSON 长度大约不能超过 30KB,超长文本我们先截断
        """
        # 粗略防御:markdown 内容超过 20KB 就截断(给卡片 JSON 包装留余地)
        if len(md) > 20000:
            md = md[:20000] + "\n\n…(内容过长已截断)"

        card: dict = {
            "config": {"wide_screen_mode": True},
            "elements": [
                {"tag": "markdown", "content": md},
            ],
        }
        if title:
            card["header"] = {
                "title": {"tag": "plain_text", "content": title},
                "template": "blue",
            }

        return await self._create_message(
            receive_id_type="open_id",
            receive_id=open_id,
            msg_type="interactive",
            content=json.dumps(card, ensure_ascii=False),
        )

    async def send_browser_approval_card(
        self,
        open_id: str,
        *,
        reason: str,
        trust_note: Optional[str] = None,
    ) -> Optional[str]:
        """发送浏览器授权卡片,按钮确认,文本命令兜底。"""
        reason = reason.strip() or "需要一个真实浏览器来继续操作"
        trust_note = (trust_note or "").strip()
        md = (
            "🌐 **当前任务需要使用浏览器**\n"
            f"原因: {reason}\n\n"
        )
        if trust_note:
            md += f"{trust_note}\n\n"
        md += (
            "点击下面按钮授权。\n"
            "如果卡片按钮失效,也可以回复 `/browser yes` 或 `/browser no`。"
        )
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "浏览器授权"},
                "template": "blue",
            },
            "elements": [
                {"tag": "markdown", "content": md},
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "允许"},
                            "type": "primary",
                            "value": {"kind": "browser_approval", "decision": "yes"},
                        },
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "拒绝"},
                            "type": "default",
                            "value": {"kind": "browser_approval", "decision": "no"},
                        },
                    ],
                },
            ],
        }
        return await self._create_message(
            receive_id_type="open_id",
            receive_id=open_id,
            msg_type="interactive",
            content=json.dumps(card, ensure_ascii=False),
        )

    async def get_user_display_name(self, open_id: str) -> Optional[str]:
        """按 open_id 查询飞书用户展示名。失败时返回 None。"""
        req = (
            GetUserRequest.builder()
            .user_id(open_id)
            .user_id_type("open_id")
            .department_id_type("open_department_id")
            .build()
        )
        resp = await self.client.contact.v3.user.aget(req)
        if not resp.success():
            logger.warning(
                "get_user_display_name failed: open_id=%s code=%s msg=%s",
                open_id,
                getattr(resp, "code", None),
                getattr(resp, "msg", None),
            )
            return None
        data = getattr(resp, "data", None)
        user = getattr(data, "user", None) if data else None
        name = getattr(user, "name", None) if user else None
        return str(name).strip() or None

    # ---------- 文件 / 图片 ----------

    # 飞书限制:单张图片 ≤ 10MB,单个文件 ≤ 30MB
    MAX_IMAGE_BYTES = 10 * 1024 * 1024
    MAX_FILE_BYTES = 30 * 1024 * 1024

    # 扩展名到 file_type 的映射;未知类型走 "stream"
    _FILE_TYPE_MAP = {
        "pdf": "pdf",
        "doc": "doc", "docx": "doc",
        "xls": "xls", "xlsx": "xls",
        "ppt": "ppt", "pptx": "ppt",
        "mp4": "mp4", "mov": "mp4",
        "opus": "opus",
    }

    _IMAGE_EXT = {"png", "jpg", "jpeg", "gif", "webp", "bmp"}

    @classmethod
    def is_image(cls, file_path: str) -> bool:
        ext = Path(file_path).suffix.lstrip(".").lower()
        return ext in cls._IMAGE_EXT

    async def upload_image(self, file_path: str) -> Optional[str]:
        """上传图片到飞书,返回 image_key。"""
        p = Path(file_path)
        if not p.is_file():
            logger.error("upload_image: file not found %s", file_path)
            return None
        size = p.stat().st_size
        if size > self.MAX_IMAGE_BYTES:
            logger.error(
                "upload_image: %s too large (%d bytes > %d)",
                file_path, size, self.MAX_IMAGE_BYTES,
            )
            return None
        with open(p, "rb") as f:
            req = (
                CreateImageRequest.builder()
                .request_body(
                    CreateImageRequestBody.builder()
                    .image_type("message")
                    .image(f)
                    .build()
                )
                .build()
            )
            resp = await self.client.im.v1.image.acreate(req)
        if not resp.success():
            logger.error(
                "upload_image failed: code=%s msg=%s",
                resp.code, resp.msg,
            )
            return None
        return resp.data.image_key if resp.data else None

    async def upload_file(self, file_path: str) -> Optional[str]:
        """上传普通文件到飞书,返回 file_key。"""
        p = Path(file_path)
        if not p.is_file():
            logger.error("upload_file: file not found %s", file_path)
            return None
        size = p.stat().st_size
        if size > self.MAX_FILE_BYTES:
            logger.error(
                "upload_file: %s too large (%d bytes > %d)",
                file_path, size, self.MAX_FILE_BYTES,
            )
            return None
        ext = p.suffix.lstrip(".").lower()
        file_type = self._FILE_TYPE_MAP.get(ext, "stream")
        with open(p, "rb") as f:
            req = (
                CreateFileRequest.builder()
                .request_body(
                    CreateFileRequestBody.builder()
                    .file_type(file_type)
                    .file_name(p.name)
                    .file(f)
                    .build()
                )
                .build()
            )
            resp = await self.client.im.v1.file.acreate(req)
        if not resp.success():
            logger.error(
                "upload_file failed: code=%s msg=%s",
                resp.code, resp.msg,
            )
            return None
        return resp.data.file_key if resp.data else None

    async def send_image(self, open_id: str, image_key: str) -> Optional[str]:
        return await self._create_message(
            receive_id_type="open_id",
            receive_id=open_id,
            msg_type="image",
            content=json.dumps({"image_key": image_key}, ensure_ascii=False),
        )

    async def send_file(self, open_id: str, file_key: str) -> Optional[str]:
        return await self._create_message(
            receive_id_type="open_id",
            receive_id=open_id,
            msg_type="file",
            content=json.dumps({"file_key": file_key}, ensure_ascii=False),
        )

    async def download_message_resource(
        self,
        *,
        message_id: str,
        file_key: str,
        resource_type: str,
        destination: Path,
    ) -> Optional[Path]:
        destination.parent.mkdir(parents=True, exist_ok=True)
        req = (
            GetMessageResourceRequest.builder()
            .message_id(message_id)
            .file_key(file_key)
            .type(resource_type)
            .build()
        )
        resp = await self.client.im.v1.message_resource.aget(req)
        if not resp.success():
            logger.error(
                "download message resource failed: code=%s msg=%s",
                resp.code,
                resp.msg,
            )
            return None

        destination.write_bytes(resp.file.read())
        return destination

    # ---------- 编辑消息 ----------

    async def update_text(self, message_id: str, text: str) -> bool:
        """编辑已发出的消息(飞书要求消息发出后一段时间内才能编辑)。"""
        if len(text) > 28000:
            text = text[:28000] + "\n\n…(消息过长已截断)"

        req = (
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                PatchMessageRequestBody.builder()
                .content(json.dumps({"text": text}, ensure_ascii=False))
                .build()
            )
            .build()
        )
        resp = await self.client.im.v1.message.apatch(req)
        if not resp.success():
            logger.error(
                "patch message failed: code=%s msg=%s", resp.code, resp.msg
            )
            return False
        return True

    # ---------- 内部 ----------

    async def _create_message(
        self,
        receive_id_type: str,
        receive_id: str,
        msg_type: str,
        content: str,
    ) -> Optional[str]:
        req = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(receive_id)
                .msg_type(msg_type)
                .content(content)
                .build()
            )
            .build()
        )
        resp: CreateMessageResponse = await self.client.im.v1.message.acreate(req)
        if not resp.success():
            logger.error(
                "create message failed: code=%s msg=%s log_id=%s",
                resp.code,
                resp.msg,
                resp.get_log_id(),
            )
            return None
        return resp.data.message_id if resp.data else None


# 全局单例
feishu_client = FeishuClient()
