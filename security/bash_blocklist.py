"""Bash 命令黑名单。

设计参考 cc-src/tools/BashTool/bashSecurity.ts 的思路:
- 不试图穷举"危险命令"(攻击者总能绕过)
- 而是穷举"已知会出大事的模式",拦住明显的灾难
- 其余放行(因为我们目标是"对齐 Claude Code 能力",过度限制会让机器人变废)

返回 (allowed, deny_reason):
    allowed=True 时 deny_reason 是 None
    allowed=False 时 deny_reason 是给 Claude 的解释,Claude 可以换种方式
"""
from __future__ import annotations

import re
import shlex
from typing import Optional, Tuple

# ---------- 内网 IP/主机名 ----------

_PRIVATE_HOST_PATTERNS = [
    r"127\.\d+\.\d+\.\d+",
    r"localhost",
    r"0\.0\.0\.0",
    r"169\.254\.\d+\.\d+",       # 链路本地(包括云厂商 metadata)
    r"10\.\d+\.\d+\.\d+",
    r"192\.168\.\d+\.\d+",
    r"172\.(1[6-9]|2\d|3[01])\.\d+\.\d+",
    r"\.local\b",
    r"\.internal\b",
    r"metadata\.google\.internal",
    r"169\.254\.169\.254",       # AWS/GCP/Azure metadata 服务
]
_PRIVATE_HOST_RE = re.compile("|".join(_PRIVATE_HOST_PATTERNS), re.IGNORECASE)

# ---------- 系统目录(不允许写) ----------

_SYSTEM_PATHS_RE = re.compile(
    r"(?:^|\s)(?:/etc|/sys|/proc|/boot|/usr|/var|/root|/dev)(?:/|\s|$)",
)

# ---------- 致命模式 ----------

_FATAL_PATTERNS = [
    # 各种 rm -rf 把家底端了
    (re.compile(r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r)\s+(/|/\*|~|\$HOME|\${HOME})"),
     "rm -rf 删除根目录或家目录"),
    (re.compile(r"\brm\s+-rf\s+\.\.?\s*$"), "rm -rf 当前/上级目录"),

    # fork 炸弹
    (re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), "fork 炸弹"),

    # 管道执行远程脚本
    (re.compile(r"curl[^|]*\|\s*(sudo\s+)?(bash|sh|zsh|python\d?|perl|ruby)"),
     "curl | sh 直接执行远程脚本(用 curl -o 下载后审查再跑)"),
    (re.compile(r"wget[^|]*\|\s*(sudo\s+)?(bash|sh|zsh|python\d?|perl|ruby)"),
     "wget | sh 直接执行远程脚本"),

    # dd 写设备
    (re.compile(r"\bdd\s+.*of=/dev/"), "dd 写入设备文件"),

    # mkfs 格式化
    (re.compile(r"\bmkfs\."), "格式化文件系统"),

    # chmod -R 777 系统目录
    (re.compile(r"chmod\s+-R\s+777\s+/"), "chmod -R 777 根目录"),

    # 直接覆盖 /dev/sda 之类
    (re.compile(r">\s*/dev/(sd|nvme|hd)"), "写入块设备"),

    # 修改 cron / sshd / sudoers / systemd
    (re.compile(r"(/etc/(crontab|sudoers|ssh|systemd)|/var/spool/cron)"),
     "修改 cron / sshd / sudoers / systemd 配置"),

    # 提权
    (re.compile(r"\b(sudo|su)\s+"), "容器内不允许提权(本来也没意义)"),

    # 关机/重启
    (re.compile(r"\b(shutdown|reboot|halt|poweroff|init\s+0|init\s+6)\b"), "关机/重启"),

    # 卸载文件系统
    (re.compile(r"\bumount\s+/"), "卸载根目录"),
]


def is_command_safe(command: str) -> Tuple[bool, Optional[str]]:
    """判断 Bash 命令是否安全。"""
    if not command or not command.strip():
        return True, None

    # 1. 致命模式
    for pattern, reason in _FATAL_PATTERNS:
        if pattern.search(command):
            return False, reason

    # 2. 内网访问(curl/wget/nc/netcat/ssh/telnet 等)
    network_tools_re = re.compile(
        r"\b(curl|wget|nc|ncat|netcat|ssh|telnet|ftp|rsync|scp)\b"
    )
    if network_tools_re.search(command):
        if _PRIVATE_HOST_RE.search(command):
            return False, "禁止访问内网/本地/云厂商 metadata 地址"

    # 3. 写入系统目录(只对 > >> tee 等明显写入操作严格)
    write_redirect_re = re.compile(r"(>>?|tee\s+(-[a-zA-Z]*\s+)?)\s*(/etc|/sys|/proc|/boot|/usr|/var|/root|/dev)")
    if write_redirect_re.search(command):
        return False, "禁止写入系统目录(/etc /sys /proc /boot /usr /var /root /dev)"

    # 4. 危险的 find -delete / find -exec rm
    if re.search(r"find\s+/[^\s]*.*-delete", command):
        return False, "find -delete 在根路径下,可能误删大量文件"
    if re.search(r"find\s+/[^\s]*.*-exec\s+rm", command):
        return False, "find -exec rm 在根路径下,可能误删大量文件"

    return True, None


def is_command_safe_tokenized(command: str) -> Tuple[bool, Optional[str]]:
    """更严格的版本:用 shlex 解析后再次校验,捕获引号内的 trick。

    第一版直接调 is_command_safe 即可。这个函数留作未来扩展点。
    """
    safe, reason = is_command_safe(command)
    if not safe:
        return safe, reason

    # 解析后重新拼接,再扫一次(应对 r"m -rf /" 这种引号 trick)
    try:
        tokens = shlex.split(command, posix=True)
        rejoined = " ".join(tokens)
        return is_command_safe(rejoined)
    except ValueError:
        # shlex 解析失败(引号不闭合等),保守放行原命令的判断
        return True, None
