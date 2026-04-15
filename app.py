"""FastAPI 入口。

职责:
  - 接收飞书 webhook,3 秒内 ack
  - URL 验证握手
  - 事件去重 + 私聊过滤
  - 命令分发(/project / /stop / /cron / 其他 → agent)
  - 异步 spawn agent 任务,不阻塞 webhook
  - 启动时初始化 scheduler 并恢复持久化的定时任务
"""
from __future__ import annotations

import asyncio
import logging

from auth import store as auth_store
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agent import browser_approval
from agent.browser_client import browser_client
from config import settings
from feishu import events as feishu_events
from feishu.client import feishu_client
from project import manager as project_manager

logger = logging.getLogger(__name__)


def _setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


_setup_logging()
settings.ensure_dirs()


app = FastAPI(title="feishu-cc", version="0.1.0")


# ---------- 启动/关闭钩子 ----------

@app.on_event("startup")
async def on_startup() -> None:
    auth_store._ensure_schema()
    from scheduler import store as scheduler_store
    from scheduler import runner as scheduler_runner
    from project import state as project_state

    # 显式触发两个 store 的 schema 初始化,不依赖懒加载顺序
    scheduler_store._ensure_meta_schema()
    project_state._ensure_schema()

    sched = scheduler_store.get_scheduler()
    sched.start()
    scheduler_runner.restore_jobs_on_startup()
    logger.info("feishu-cc started")


@app.on_event("shutdown")
async def on_shutdown() -> None:
    from agent import runner as agent_runner
    from scheduler import store as scheduler_store

    try:
        scheduler_store.get_scheduler().shutdown(wait=False)
    except Exception:
        pass
    await agent_runner.shutdown_all()
    logger.info("feishu-cc stopped")


# ---------- 健康检查 ----------

@app.get("/health")
async def health() -> dict:
    return {"ok": True}


# ---------- 飞书 webhook ----------

@app.post("/feishu/webhook")
async def feishu_webhook(request: Request) -> JSONResponse:
    raw = await request.body()

    try:
        body = feishu_events.decrypt_event(raw)
    except Exception as exc:
        logger.exception("decrypt failed")
        return JSONResponse({"error": str(exc)}, status_code=400)

    # URL 验证(首次配 webhook 时握手)
    if feishu_events.is_url_verification(body):
        return JSONResponse(feishu_events.url_verification_response(body))

    # token 校验(基础防御:确认是飞书发来的)
    if settings.feishu_verification_token:
        header = body.get("header") or {}
        if header.get("token") != settings.feishu_verification_token:
            logger.warning("invalid verification token")
            return JSONResponse({"error": "invalid token"}, status_code=401)

    # 事件去重
    event_id = (body.get("header") or {}).get("event_id", "")
    if feishu_events.is_duplicate(event_id):
        return JSONResponse({"ok": True})

    # 解析消息事件
    parsed = feishu_events.parse_message_event(body)
    if not parsed:
        return JSONResponse({"ok": True})

    # 准入校验
    if not feishu_events.is_allowed(parsed):
        # 安静地忽略,不回复(避免 spam)
        return JSONResponse({"ok": True})

    # 异步处理,立刻 ack
    asyncio.create_task(_dispatch(parsed))
    return JSONResponse({"ok": True})


# ---------- 命令分发 ----------

async def _dispatch(parsed: feishu_events.ParsedMessageEvent) -> None:
    """根据消息内容路由到 project / stop / cron / agent。"""
    open_id = parsed.sender_open_id
    text = parsed.text.strip()

    try:
        is_admin = auth_store.is_admin(open_id)
        access_status = auth_store.get_access_status(open_id)

        if text == "/whoami":
            await feishu_client.send_text(open_id, f"你的 open_id:{open_id}")
            return

        if text in ("/help", "/?"):
            await feishu_client.send_text(
                open_id,
                _help_text(is_admin=is_admin, approved=(is_admin or access_status == "approved")),
            )
            return

        if text == "/apply":
            await _handle_apply_command(open_id, access_status)
            return

        if text == "/status":
            await feishu_client.send_text(open_id, _access_status_text(access_status))
            return

        if is_admin and text.startswith("/approve"):
            await _handle_approve_command(open_id, text)
            return

        if is_admin and text.startswith("/reject"):
            await _handle_reject_command(open_id, text)
            return

        if not is_admin and access_status != "approved":
            await feishu_client.send_text(open_id, _access_required_text(access_status))
            return

        if text.startswith("/browser"):
            await _handle_browser_command(open_id, text)
            return

        # /stop 中断当前 agent 任务
        if text in ("/stop", "/cancel", "/中断"):
            from agent import runner as agent_runner

            interrupted = await agent_runner.interrupt_user(open_id)
            if interrupted:
                await feishu_client.send_text(open_id, "🛑 正在中断…")
            else:
                await feishu_client.send_text(open_id, "ℹ️ 当前没有正在运行的任务。")
            return

        # /cron list / /cron delete <id>(查看和管理定时任务)
        if text.startswith("/cron"):
            await _handle_cron_command(open_id, text)
            return

        # /project 系列
        if project_manager.is_project_command(text):
            result = await project_manager.handle_project_command(open_id, text)
            await feishu_client.send_text(open_id, result.text)
            return

        from agent import runner as agent_runner

        if parsed.attachments:
            await agent_runner.handle_incoming_message(
                open_id,
                text=text,
                message_id=parsed.message_id,
                attachments=parsed.attachments,
            )
            return

        # 其他全部丢给 agent
        if not text:
            return
        await agent_runner.handle_user_message(open_id, text)

    except Exception as exc:
        logger.exception("dispatch failed")
        try:
            from agent import runner as agent_runner
            await feishu_client.send_text(
                open_id, agent_runner._format_error_for_user(exc, open_id)
            )
        except Exception:
            pass


async def _handle_cron_command(open_id: str, text: str) -> None:
    parts = text.split(maxsplit=2)
    sub = parts[1].lower() if len(parts) >= 2 else "list"

    from scheduler import store as scheduler_store

    if sub == "list":
        tasks = scheduler_store.list_tasks(open_id)
        if not tasks:
            await feishu_client.send_text(open_id, "⏰ 你还没有任何定时任务。")
            return
        lines = ["⏰ 你的定时任务:"]
        for t in tasks:
            lines.append(
                f"  #{t.task_id}  cron={t.cron_expr}  项目={t.project}\n"
                f"     {t.note or t.prompt[:80]}"
            )
        await feishu_client.send_text(open_id, "\n".join(lines))
        return

    if sub == "delete":
        if len(parts) < 3:
            await feishu_client.send_text(open_id, "用法:/cron delete <task_id>")
            return
        task_id = parts[2].strip()
        ok = scheduler_store.delete_task(task_id, open_id)
        if ok:
            scheduler_store.unschedule_job(task_id)
            await feishu_client.send_text(open_id, f"🗑 已删除定时任务 #{task_id}")
        else:
            await feishu_client.send_text(open_id, f"未找到任务 #{task_id}")
        return

    await feishu_client.send_text(
        open_id,
        "⏰ 定时任务命令\n"
        "/cron list                 列出所有定时任务\n"
        "/cron delete <task_id>     删除某个定时任务\n"
        "(创建定时任务请直接告诉我 — 比如『每天早上 8 点检查 GitHub 新 issue』)"
    )


async def _handle_browser_command(open_id: str, text: str) -> None:
    parts = text.split(maxsplit=1)
    sub = parts[1].strip().lower() if len(parts) >= 2 else "status"

    if sub == "yes":
        ok = browser_approval.resolve_request(open_id, approved=True)
        if ok:
            await feishu_client.send_text(open_id, "✅ 已允许 agent 使用浏览器。")
        else:
            await feishu_client.send_text(open_id, "ℹ️ 当前没有待确认的浏览器请求。")
        return

    if sub == "no":
        ok = browser_approval.resolve_request(open_id, approved=False)
        if ok:
            await feishu_client.send_text(open_id, "🛑 已取消本次浏览器请求。")
        else:
            await feishu_client.send_text(open_id, "ℹ️ 当前没有待确认的浏览器请求。")
        return

    if sub == "status":
        try:
            session = await browser_client.get_session(open_id)
        except Exception as exc:
            await feishu_client.send_text(open_id, f"❌ 查询浏览器状态失败:{exc}")
            return
        if not session:
            await feishu_client.send_text(open_id, "ℹ️ 当前没有浏览器会话。")
            return
        message = [
            "🌐 浏览器状态",
            f"状态: {session.get('state', 'unknown')}",
            f"控制方: {session.get('controller', 'unknown')}",
        ]
        if session.get("queue_position"):
            message.append(f"排队位置: {session['queue_position']}")
        if session.get("viewer_url"):
            message.append(f"旁观/接管链接: {session['viewer_url']}")
        await feishu_client.send_text(open_id, "\n".join(message))
        return

    if sub == "close":
        try:
            result = await browser_client.close_session(open_id)
        except Exception as exc:
            await feishu_client.send_text(open_id, f"❌ 关闭浏览器会话失败:{exc}")
            return
        if not result:
            await feishu_client.send_text(open_id, "ℹ️ 当前没有可关闭的浏览器会话。")
            return
        await feishu_client.send_text(open_id, "🧹 已关闭当前浏览器会话。")
        return

    await feishu_client.send_text(
        open_id,
        "🌐 浏览器命令\n"
        "/browser yes       允许 agent 使用浏览器\n"
        "/browser no        拒绝本次浏览器请求\n"
        "/browser status    查看当前浏览器会话状态\n"
        "/browser close     关闭当前浏览器会话",
    )


async def _handle_apply_command(open_id: str, access_status: str) -> None:
    before = access_status
    user = auth_store.request_access(open_id)

    if user.is_admin or user.status == "approved":
        await feishu_client.send_text(open_id, "✅ 你已经开通，可以直接使用这个 bot。")
        return

    if before == "pending":
        await feishu_client.send_text(open_id, "🕒 你的申请还在审批中，请等待管理员处理。")
        return

    await feishu_client.send_text(
        open_id,
        "📝 已提交开通申请。审批通过后，我会主动通知你。",
    )
    await _notify_admins(
        "[审批] 收到新的开通申请\n"
        f"open_id: {open_id}\n"
        f"批准: /approve {open_id}\n"
        f"拒绝: /reject {open_id} 原因",
    )


async def _handle_approve_command(admin_open_id: str, text: str) -> None:
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await feishu_client.send_text(admin_open_id, "用法:/approve <open_id>")
        return

    target_open_id = parts[1].strip()
    auth_store.approve_user(target_open_id, admin_open_id)
    await feishu_client.send_text(
        target_open_id,
        "✅ 你的使用权限已开通。现在可以直接给我发消息了。",
    )
    await feishu_client.send_text(
        admin_open_id,
        f"[审批] 已批准 {target_open_id}",
    )


async def _handle_reject_command(admin_open_id: str, text: str) -> None:
    parts = text.split(maxsplit=2)
    if len(parts) < 2 or not parts[1].strip():
        await feishu_client.send_text(admin_open_id, "用法:/reject <open_id> [原因]")
        return

    target_open_id = parts[1].strip()
    reason = parts[2].strip() if len(parts) >= 3 else ""
    auth_store.reject_user(target_open_id, admin_open_id, reason or None)

    message = "❌ 你的开通申请未通过。"
    if reason:
        message += f"\n原因:{reason}"
    message += "\n如需重新申请，发送 /apply 即可。"
    await feishu_client.send_text(target_open_id, message)
    await feishu_client.send_text(
        admin_open_id,
        f"[审批] 已拒绝 {target_open_id}" + (f"\n原因:{reason}" if reason else ""),
    )


async def _notify_admins(text: str) -> None:
    for admin_open_id in auth_store.list_admin_open_ids():
        await feishu_client.send_text(admin_open_id, text)


def _access_required_text(access_status: str) -> str:
    if access_status == "pending":
        return "🕒 你还在审批中。发送 /status 查看状态。"
    if access_status == "rejected":
        return "❌ 你当前未开通。发送 /apply 可重新提交申请。"
    return "🔒 你还没有开通使用权限。发送 /apply 提交申请，发送 /status 查看进度。"


def _access_status_text(access_status: str) -> str:
    if access_status == "approved":
        return "✅ 当前状态: 已开通"
    if access_status == "pending":
        return "🕒 当前状态: 审批中"
    if access_status == "rejected":
        return "❌ 当前状态: 未通过，可发送 /apply 重新申请"
    return "🔒 当前状态: 未申请，发送 /apply 提交开通申请"


def _help_text(*, is_admin: bool, approved: bool) -> str:
    if not approved:
        return (
            "🤖 飞书 Claude Code 帮助\n"
            "\n"
            "你还没有开通使用权限。可用命令:\n"
            "  /apply             提交开通申请\n"
            "  /status            查看审批状态\n"
            "  /whoami            查看自己的 open_id\n"
            "  /help              本帮助\n"
        )

    lines = [
        "🤖 飞书 Claude Code 帮助",
        "",
        "直接发消息就是跟 Claude 对话。常用命令:",
        "  /project           项目管理(list/switch/new/clone/delete)",
        "  /cron              定时任务管理",
        "  /browser           浏览器授权/状态管理",
        "  /stop              中断当前正在跑的任务",
        "  /status            查看自己的开通状态",
        "  /whoami            查看自己的 open_id",
        "  /help              本帮助",
    ]
    if is_admin:
        lines.extend(
            [
                "",
                "管理员命令:",
                "  /approve <open_id>         批准用户开通",
                "  /reject <open_id> [原因]   拒绝用户开通",
            ]
        )
    lines.extend(
        [
            "",
            "首次使用默认在 scratch 项目里;输入 /project new 我的项目 创建一个正式项目。",
        ]
    )
    return "\n".join(lines)
