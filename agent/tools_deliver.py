"""自定义 MCP 工具:让 Claude 把沙盒里的文件真正"交付"给飞书用户。

设计要点:
  - 按 open_id 构造闭包,文件只会发给那个用户自己,没法跨用户
  - 严格路径校验:必须 resolve 后仍在用户的沙盒根目录下
  - 根据扩展名自动走图片通道还是文件通道
  - 大小限制由 feishu.client 里的常量控制(图片 10MB / 文件 30MB)
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict

from claude_agent_sdk import create_sdk_mcp_server, tool

from config import settings
from feishu.client import FeishuClient, feishu_client

logger = logging.getLogger(__name__)


def _user_sandbox_root(open_id: str) -> Path:
    """用户所有项目的公共根(sandbox/users/<open_id>)。"""
    return (settings.sandbox_path / "users" / open_id).resolve()


def _is_inside(child: Path, parent: Path) -> bool:
    """child 是否是 parent 的子路径(都已 resolve)。"""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def build_deliver_mcp(open_id: str):
    """为某个用户构造 deliver MCP server。"""
    user_root = _user_sandbox_root(open_id)

    @tool(
        "deliver_file",
        (
            "Send a file from the current sandbox to the user via Feishu IM. "
            "Use this whenever the user asks for a file (HTML page, script, PDF, "
            "image, data dump, etc.) — the user cannot access your filesystem "
            "directly, so you MUST use this tool instead of just telling them a path. "
            "The path can be absolute or relative to the current working directory. "
            "Images (png/jpg/gif/webp) are sent as inline messages; everything else "
            "is sent as a downloadable file attachment. "
            "Size limits: 10MB for images, 30MB for files."
        ),
        {
            "path": str,  # 要交付的文件路径(绝对或相对 cwd)
            "caption": str,  # 可选:随文件发送的一句说明
        },
    )
    async def deliver_file(args: Dict[str, Any]) -> Dict[str, Any]:
        raw_path = (args.get("path") or "").strip()
        caption = (args.get("caption") or "").strip()
        if not raw_path:
            return _err("path is required")

        # 把相对路径映射到 cwd(os.getcwd 是 Claude 当前工作目录)
        try:
            if os.path.isabs(raw_path):
                target = Path(raw_path).resolve()
            else:
                target = (Path(os.getcwd()) / raw_path).resolve()
        except Exception as exc:
            return _err(f"invalid path: {exc}")

        # 路径必须在用户沙盒根内
        if not _is_inside(target, user_root):
            logger.warning(
                "deliver_file: path %s escapes user sandbox %s",
                target, user_root,
            )
            return _err(
                f"path {target} is outside your sandbox ({user_root}). "
                "You can only deliver files you created in your own working directory."
            )

        if not target.is_file():
            return _err(f"file not found: {target}")

        try:
            size = target.stat().st_size
        except Exception as exc:
            return _err(f"cannot stat file: {exc}")

        is_image = FeishuClient.is_image(str(target))
        limit = FeishuClient.MAX_IMAGE_BYTES if is_image else FeishuClient.MAX_FILE_BYTES
        if size > limit:
            return _err(
                f"file is {size} bytes, over the {limit}-byte limit "
                f"({'image' if is_image else 'file'}). Split it or compress it."
            )

        # 可选:先发一条说明文字
        if caption:
            await feishu_client.send_text(open_id, caption)

        if is_image:
            key = await feishu_client.upload_image(str(target))
            if not key:
                return _err("feishu upload_image returned no key (check logs)")
            msg_id = await feishu_client.send_image(open_id, key)
        else:
            key = await feishu_client.upload_file(str(target))
            if not key:
                return _err("feishu upload_file returned no key (check logs)")
            msg_id = await feishu_client.send_file(open_id, key)

        if not msg_id:
            return _err("feishu send_message returned no message_id (check logs)")

        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"✅ Delivered {target.name} "
                        f"({'image' if is_image else 'file'}, {size} bytes) "
                        "to the user via Feishu."
                    ),
                }
            ]
        }

    return create_sdk_mcp_server(
        name="deliver",
        version="1.0.0",
        tools=[deliver_file],
    )


def _err(msg: str) -> Dict[str, Any]:
    return {
        "content": [{"type": "text", "text": f"Error: {msg}"}],
        "is_error": True,
    }
