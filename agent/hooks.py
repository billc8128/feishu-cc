"""SDK hooks 配置。

用 PreToolUse 拦截危险 Bash 命令(调用 security.bash_blocklist)。
所有拦截事件追加到审计日志,方便事后排查。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List

from claude_agent_sdk import HookMatcher

from config import settings
from security.bash_blocklist import is_command_safe_tokenized

logger = logging.getLogger(__name__)


def _audit(line: str) -> None:
    try:
        settings.ensure_dirs()
        with open(settings.audit_log_path, "a", encoding="utf-8") as f:
            f.write(f"{datetime.utcnow().isoformat()}Z {line}\n")
    except Exception as exc:
        logger.warning("audit write failed: %s", exc)


def build_hooks(open_id: str) -> Dict[str, List[HookMatcher]]:
    """为某个用户构造 hook 配置。open_id 用于审计日志归属。"""

    async def pre_bash(
        input_data: Dict[str, Any], tool_use_id: str | None, context: Any
    ) -> Dict[str, Any]:
        tool_input = input_data.get("tool_input", {}) or {}
        command = tool_input.get("command", "")

        safe, reason = is_command_safe_tokenized(command)
        if not safe:
            _audit(f"BLOCK open_id={open_id} reason={reason!r} cmd={command!r}")
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        f"❌ 命令被安全策略拦截:{reason}\n\n"
                        "请换一种方式实现,比如先把要操作的目标限定到工作目录内,"
                        "或者把目标资源放到允许的位置后再操作。"
                    ),
                }
            }

        # 放行,记日志
        _audit(f"ALLOW open_id={open_id} cmd={command!r}")
        return {}

    async def pre_write(
        input_data: Dict[str, Any], tool_use_id: str | None, context: Any
    ) -> Dict[str, Any]:
        """Write/Edit 工具:拦截写到工作目录之外的路径。"""
        tool_input = input_data.get("tool_input", {}) or {}
        path = tool_input.get("file_path", "")
        if not path:
            return {}

        # 简单防御:禁止写到系统目录
        forbidden_prefixes = ("/etc", "/sys", "/proc", "/boot", "/usr", "/var", "/root", "/dev")
        if any(path.startswith(p + "/") or path == p for p in forbidden_prefixes):
            _audit(f"BLOCK open_id={open_id} write_to_system path={path!r}")
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        f"❌ 不允许写入系统目录:{path}\n请写到当前项目目录内。"
                    ),
                }
            }
        return {}

    return {
        "PreToolUse": [
            HookMatcher(matcher="Bash", hooks=[pre_bash]),
            HookMatcher(matcher="Write|Edit", hooks=[pre_write]),
        ]
    }
