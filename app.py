"""FastAPI 入口。

职责:
  - 接收飞书 webhook,3 秒内 ack
  - URL 验证握手
  - 事件去重 + 白名单
  - 命令分发(/project / /stop / /cron / 其他 → agent)
  - 异步 spawn agent 任务,不阻塞 webhook
  - 启动时初始化 scheduler 并恢复持久化的定时任务
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

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
    from scheduler import store as scheduler_store
    from scheduler import runner as scheduler_runner

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
        # /stop 中断当前 agent 任务
        if text in ("/stop", "/cancel", "/中断"):
            from agent import runner as agent_runner

            interrupted = await agent_runner.interrupt_user(open_id)
            if interrupted:
                await feishu_client.send_text(open_id, "🛑 正在中断…")
            else:
                await feishu_client.send_text(open_id, "ℹ️ 当前没有正在运行的任务。")
            return

        # /whoami 显示自己的 open_id(便于配置白名单)
        if text == "/whoami":
            await feishu_client.send_text(open_id, f"你的 open_id:{open_id}")
            return

        # /help
        if text in ("/help", "/?"):
            await feishu_client.send_text(open_id, _help_text())
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

        # 其他全部丢给 agent
        if not text:
            return
        from agent import runner as agent_runner

        await agent_runner.handle_user_message(open_id, text)

    except Exception as exc:
        logger.exception("dispatch failed")
        try:
            await feishu_client.send_text(open_id, f"❌ 内部错误:{exc}")
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


def _help_text() -> str:
    return (
        "🤖 飞书 Claude Code 帮助\n"
        "\n"
        "直接发消息就是跟 Claude 对话。常用命令:\n"
        "  /project           项目管理(list/switch/new/clone/delete)\n"
        "  /cron              定时任务管理\n"
        "  /stop              中断当前正在跑的任务\n"
        "  /whoami            查看自己的 open_id\n"
        "  /help              本帮助\n"
        "\n"
        "首次使用默认在 scratch 项目里;输入 /project new 我的项目 创建一个正式项目。"
    )
