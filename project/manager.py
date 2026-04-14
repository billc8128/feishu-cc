"""多项目管理:/project list / switch / new / clone / current / delete

每个项目对应一个工作目录。首版按 open_id 命名空间隔离。
"""
from __future__ import annotations

import asyncio
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import List

from config import settings
from project import state

_VALID_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_\-]{0,63}$")
# git URL 白名单:仅放行常见 SSH/HTTPS 形式,杜绝奇怪的 schema
_VALID_GIT_URL = re.compile(
    r"^(https?://|git@)[A-Za-z0-9._\-]+[:/][A-Za-z0-9._/\-]+(\.git)?/?$"
)


@dataclass
class CommandResult:
    text: str
    success: bool = True


def get_project_root(open_id: str, project_name: str) -> Path:
    """计算某用户某项目的工作目录。

    首版命名空间约定:`{sandbox}/users/{open_id}/{project_name}`。
    "users/" 这一层是给未来扩多用户/群聊预留的。
    """
    return settings.sandbox_path / "users" / open_id / project_name


def ensure_project_root(open_id: str, project_name: str) -> Path:
    root = get_project_root(open_id, project_name)
    root.mkdir(parents=True, exist_ok=True)
    return root


def list_projects(open_id: str) -> List[str]:
    user_root = settings.sandbox_path / "users" / open_id
    if not user_root.exists():
        return []
    return sorted([p.name for p in user_root.iterdir() if p.is_dir()])


# ---------- 命令处理 ----------

def is_project_command(text: str) -> bool:
    return text.strip().startswith("/project")


async def handle_project_command(open_id: str, text: str) -> CommandResult:
    parts = text.strip().split(maxsplit=2)
    if len(parts) < 2:
        return _help()

    sub = parts[1].lower()

    if sub == "list":
        return _cmd_list(open_id)
    if sub == "current":
        return _cmd_current(open_id)
    if sub == "switch":
        if len(parts) < 3:
            return CommandResult("用法:/project switch <项目名>", success=False)
        return _cmd_switch(open_id, parts[2].strip())
    if sub == "new":
        if len(parts) < 3:
            return CommandResult("用法:/project new <项目名>", success=False)
        return _cmd_new(open_id, parts[2].strip())
    if sub == "clone":
        if len(parts) < 3:
            return CommandResult("用法:/project clone <git-url> [项目名]", success=False)
        return await _cmd_clone(open_id, parts[2].strip())
    if sub == "delete":
        if len(parts) < 3:
            return CommandResult("用法:/project delete <项目名>", success=False)
        return _cmd_delete(open_id, parts[2].strip())
    if sub in ("help", "?"):
        return _help()

    return CommandResult(f"未知子命令:{sub}\n\n{_help().text}", success=False)


def _help() -> CommandResult:
    return CommandResult(
        "📁 项目管理命令\n"
        "/project list                 列出所有项目\n"
        "/project current              查看当前项目\n"
        "/project switch <名字>        切换到某个项目\n"
        "/project new <名字>           新建空项目\n"
        "/project clone <git-url>      从 git 克隆\n"
        "/project delete <名字>        删除项目(不可恢复)\n"
        "\n首次使用时默认在 scratch(草稿)项目里。"
    )


def _cmd_list(open_id: str) -> CommandResult:
    projects = list_projects(open_id)
    current = state.get_current_project(open_id)
    if not projects:
        return CommandResult(
            f"还没有任何项目。\n当前活动:{current}(空)\n输入 /project new <名字> 新建一个。"
        )
    lines = ["📁 你的项目:"]
    for name in projects:
        marker = " ← 当前" if name == current else ""
        lines.append(f"  • {name}{marker}")
    return CommandResult("\n".join(lines))


def _cmd_current(open_id: str) -> CommandResult:
    name = state.get_current_project(open_id)
    root = ensure_project_root(open_id, name)
    return CommandResult(f"📍 当前项目:{name}\n📂 路径:{root}")


def _cmd_switch(open_id: str, name: str) -> CommandResult:
    if not _VALID_NAME.match(name):
        return CommandResult(
            "项目名只能包含字母数字下划线短横,且不能以符号开头。",
            success=False,
        )
    root = get_project_root(open_id, name)
    if not root.exists():
        return CommandResult(
            f"项目 '{name}' 不存在。\n用 /project new {name} 创建。",
            success=False,
        )
    state.set_current_project(open_id, name)
    return CommandResult(f"✅ 已切换到项目:{name}")


def _cmd_new(open_id: str, name: str) -> CommandResult:
    if not _VALID_NAME.match(name):
        return CommandResult(
            "项目名只能包含字母数字下划线短横,且不能以符号开头。",
            success=False,
        )
    root = get_project_root(open_id, name)
    if root.exists():
        return CommandResult(
            f"项目 '{name}' 已存在,直接 /project switch {name}。",
            success=False,
        )
    root.mkdir(parents=True, exist_ok=True)
    state.set_current_project(open_id, name)
    return CommandResult(f"✅ 已创建项目 {name} 并切换。\n📂 路径:{root}")


async def _cmd_clone(open_id: str, args: str) -> CommandResult:
    tokens = args.split()
    if not tokens:
        return CommandResult("用法:/project clone <git-url> [项目名]", success=False)
    git_url = tokens[0]
    if not _VALID_GIT_URL.match(git_url):
        return CommandResult(
            "URL 格式不合法,只支持 https://... 或 git@...:.../...git",
            success=False,
        )
    if len(tokens) >= 2:
        name = tokens[1]
    else:
        name = _infer_name_from_url(git_url)
    if not _VALID_NAME.match(name):
        return CommandResult(
            f"无法从 URL 推出合法项目名,显式传:/project clone {git_url} <名字>",
            success=False,
        )
    root = get_project_root(open_id, name)
    if root.exists():
        return CommandResult(
            f"项目 '{name}' 已存在,先 /project delete {name} 或换个名字。",
            success=False,
        )
    root.parent.mkdir(parents=True, exist_ok=True)

    # 用参数列表形式调用 git,不走 shell,无注入风险
    proc = await asyncio.create_subprocess_exec(
        "git",
        "clone",
        "--depth",
        "50",
        "--",
        git_url,
        str(root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        if root.exists():
            shutil.rmtree(root, ignore_errors=True)
        err = (stderr or b"").decode("utf-8", errors="replace")[:1500]
        return CommandResult(f"❌ 克隆失败:\n{err}", success=False)

    state.set_current_project(open_id, name)
    return CommandResult(f"✅ 已克隆 {git_url} 到项目 {name} 并切换。")


def _cmd_delete(open_id: str, name: str) -> CommandResult:
    if not _VALID_NAME.match(name):
        return CommandResult("项目名格式非法。", success=False)
    if name == "scratch":
        return CommandResult(
            "scratch 是默认项目,不能删除(可以手动清空内容)。", success=False
        )
    root = get_project_root(open_id, name)
    if not root.exists():
        return CommandResult(f"项目 '{name}' 不存在。", success=False)
    shutil.rmtree(root, ignore_errors=True)
    state.clear_session_id(open_id, name)
    if state.get_current_project(open_id) == name:
        state.set_current_project(open_id, "scratch")
        ensure_project_root(open_id, "scratch")
    return CommandResult(f"🗑 已删除项目 {name}。")


def _infer_name_from_url(url: str) -> str:
    name = url.rstrip("/").rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return name
