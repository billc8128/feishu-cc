"""跨模块复用的沙盒路径校验。

tools_deliver 和 tools_docs 都需要"用户给了个路径,校验它在该用户沙盒内"这种操作。
抽成一个 helper 防止两份实现漂移。

关键不变量:
  - 相对路径解析时相对 sandbox_root(不是进程 CWD)—— agent 的 cwd 可能随项目切换,
    但沙盒根是稳定的。
  - 解析必须在 _is_inside 检查之前,让 symlink 指向外部时能被识破。
"""
from __future__ import annotations

from pathlib import Path


def is_inside(child: Path, parent: Path) -> bool:
    """child 是否在 parent 下(两者都应已 .resolve() 过)。"""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_sandbox_path(raw: str | Path, sandbox_root: Path) -> Path:
    """把 raw 解析为绝对路径并校验在 sandbox_root 内,返回 .resolve() 后的 Path。

    - raw 是相对路径时,相对 sandbox_root 解析(不是 CWD)
    - symlink 会被 .resolve() 跟穿,避免软链接越狱
    - 任何越界都 raise PermissionError
    """
    p = Path(raw)
    if not p.is_absolute():
        p = sandbox_root / p
    p = p.resolve()
    root = sandbox_root.resolve()
    if not is_inside(p, root):
        raise PermissionError(f"path outside sandbox: {raw}")
    return p
