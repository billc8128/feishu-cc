"""飞书 API 封装层。只做协议适配,不含业务逻辑。

基于 lark-oapi(飞书官方 Python SDK)。所有 token 刷新、签名校验、加密解密
都由官方 SDK 处理,我们只暴露最少的、业务层需要的几个动作。
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    CreateMessageResponse,
    PatchMessageRequest,
    PatchMessageRequestBody,
)

from config import settings

logger = logging.getLogger(__name__)


class FeishuClient:
    """飞书客户端的最小封装。"""

    def __init__(self) -> None:
        # lark-oapi 的 client 是单例的,内部自动管理 tenant_access_token 的获取与刷新
        self.client = (
            lark.Client.builder()
            .app_id(settings.feishu_app_id)
            .app_secret(settings.feishu_app_secret)
            .log_level(lark.LogLevel.WARNING)
            .build()
        )

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

    async def send_markdown(self, open_id: str, title: str, md: str) -> Optional[str]:
        """发交互式卡片(用于富文本展示工具进度等)。"""
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "blue",
            },
            "elements": [
                {"tag": "markdown", "content": md},
            ],
        }
        return await self._create_message(
            receive_id_type="open_id",
            receive_id=open_id,
            msg_type="interactive",
            content=json.dumps(card, ensure_ascii=False),
        )

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
