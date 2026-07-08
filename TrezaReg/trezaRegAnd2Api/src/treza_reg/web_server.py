"""Web 控制面板 — FastAPI 后端."""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import account_manager as am
from . import generate_service as gs
from . import register_service as rs
from .config import _ROOT
from .proxy_pool import ProxyPool

_TEMPLATES = Path(__file__).resolve().parent / "templates"
_STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="TrezaReg Panel", version="0.2.0")


@app.on_event("startup")
async def _startup() -> None:
    """服务启动时根据配置自启动均衡/定时注册任务."""
    await rs.init_auto_start()

if _STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

# ---- 后台任务状态 ----
_check_status: dict[str, Any] = {"running": False, "progress": 0, "total": 0, "msg": ""}
_check_pool = ProxyPool(jobs_hint=3)


# ======================================================================
# 页面
# ======================================================================

@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return _read_html("dashboard.html")


@app.get("/register", response_class=HTMLResponse)
async def register_page() -> str:
    return _read_html("register.html")


@app.get("/generate", response_class=HTMLResponse)
async def generate_page() -> str:
    return _read_html("generate.html")


def _read_html(filename: str) -> str:
    p = _TEMPLATES / filename
    if p.exists():
        return p.read_text(encoding="utf-8")
    return f"<h1>{filename} not found</h1>"


# ======================================================================
# 统计
# ======================================================================

@app.get("/api/stats")
async def stats() -> dict[str, int]:
    return am.get_stats()


# ======================================================================
# 账号列表 (分页)
# ======================================================================

@app.get("/api/accounts")
async def list_accounts(
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=5, le=100),
    status: str = Query("all", pattern=r"^(all|available|disabled)$"),
    search: str = Query("", max_length=100),
) -> dict:
    include_disabled = (status == "disabled")
    all_accounts = am.load_accounts(include_disabled=include_disabled)

    if status == "available":
        all_accounts = [
            a for a in all_accounts
            if not a.get("_disabled") and (
                a.get("credits") is None or a.get("credits", 0) > 0
            )
        ]
    elif status == "disabled":
        all_accounts = [
            a for a in all_accounts
            if a.get("_disabled") or (a.get("credits") is not None and a.get("credits", 0) <= 0)
        ]

    if search:
        q = search.lower()
        all_accounts = [
            a for a in all_accounts
            if q in (a.get("email", "")).lower()
            or q in (a.get("wallet_address", "")).lower()
        ]

    total = len(all_accounts)
    total_pages = max(1, math.ceil(total / per_page))
    start = (page - 1) * per_page
    end = start + per_page
    page_items = all_accounts[start:end]

    return {
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "items": [_account_row(a) for a in page_items],
    }


def _account_row(a: dict) -> dict:
    return {
        "email": a.get("email", ""),
        "user_id": a.get("user_id", ""),
        "wallet_address": a.get("wallet_address", ""),
        "credits": a.get("credits"),
        "credits_checked_at": a.get("credits_checked_at", ""),
        "is_new_user": a.get("is_new_user", False),
        "registered_at": a.get("registered_at", ""),
        "created_at": a.get("created_at", 0),
        "token": a.get("token", "")[:40] + "..." if a.get("token") else "",
        "privy_access_token": (a.get("privy_access_token", "")[:40] + "..." if a.get("privy_access_token") else ""),
        "refresh_token": a.get("refresh_token", ""),
        "disabled": a.get("_disabled", False),
        "elapsed_s": a.get("elapsed_s"),
        "proxy_used": a.get("proxy_used", ""),
    }


# ======================================================================
# 额度检查
# ======================================================================

@app.post("/api/accounts/{email}/check")
async def check_one_account(email: str) -> dict:
    accounts = am.load_accounts(include_disabled=False)
    target = None
    for a in accounts:
        if a.get("email") == email:
            target = a
            break
    if not target:
        return JSONResponse({"error": "账号不存在"}, status_code=404)

    proxy = await _check_pool.get_proxy()
    try:
        updated = await am.check_and_update_account(target, proxy)
        return {
            "ok": True,
            "email": updated.get("email", email),
            "credits": updated.get("credits"),
            "wallet_address": updated.get("wallet_address", ""),
            "disabled": updated.get("_disabled", False),
            "checked_at": updated.get("credits_checked_at", ""),
        }
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    finally:
        if proxy:
            _check_pool.release(proxy)


@app.post("/api/accounts/check-all")
async def check_all_accounts() -> dict:
    global _check_status
    if _check_status["running"]:
        return {"ok": False, "error": "已有检查任务在运行"}
    return await _start_batch_check(am.load_accounts(include_disabled=False))


@app.post("/api/accounts/check-batch")
async def check_batch_accounts(data: dict) -> dict:
    """批量检查指定邮箱的额度.

    Body: {"emails": ["a@x.com", "b@x.com"]}
    """
    global _check_status
    if _check_status["running"]:
        return {"ok": False, "error": "已有检查任务在运行"}

    emails = set(data.get("emails", []))
    if not emails:
        return {"ok": False, "error": "未选择任何账号"}

    all_accs = am.load_accounts(include_disabled=False)
    targets = [a for a in all_accs if a.get("email") in emails]
    if not targets:
        return {"ok": False, "error": "未找到匹配账号"}

    return await _start_batch_check(targets)


async def _start_batch_check(accounts: list) -> dict:
    global _check_status
    _check_status = {"running": True, "progress": 0, "total": len(accounts), "msg": "开始..."}

    async def _run():
        global _check_status
        def progress(cur, tot, email, status):
            _check_status["progress"] = cur
            _check_status["total"] = tot
            _check_status["msg"] = f"{email}: {status}"

        checked = 0
        moved = 0
        for i, acct in enumerate(accounts):
            proxy = await _check_pool.get_proxy()
            try:
                updated = await am.check_and_update_account(acct, proxy)
                checked += 1
                if updated.get("_disabled"):
                    moved += 1
                progress(i + 1, len(accounts), acct.get("email", ""), "ok")
            except Exception as exc:
                progress(i + 1, len(accounts), acct.get("email", ""), str(exc))
            finally:
                if proxy:
                    _check_pool.release(proxy)
        _check_status["running"] = False
        _check_status["msg"] = f"完成: 检查 {checked} 个, 废弃 {moved} 个"

    asyncio.create_task(_run())
    return {"ok": True, "msg": f"检查任务已启动 ({len(accounts)} 个账号)"}


@app.get("/api/accounts/check-all/status")
async def check_all_status() -> dict:
    return dict(_check_status)


# ======================================================================
# 账号废弃/恢复
# ======================================================================

@app.post("/api/accounts/{email}/disable")
async def disable_account(email: str) -> dict:
    filepath = am.get_account_file(email)
    if not filepath:
        return JSONResponse({"error": "账号不存在"}, status_code=404)
    data = json.loads(filepath.read_text(encoding="utf-8"))
    data["_file"] = str(filepath)
    new_path = am.move_to_disabled(data)
    if new_path:
        return {"ok": True, "msg": f"已移至废弃: {new_path.name}"}
    return JSONResponse({"ok": False, "error": "移动失败"}, status_code=500)


@app.post("/api/accounts/{email}/restore")
async def restore_account(email: str) -> dict:
    disabled = am.load_accounts(include_disabled=True)
    target = None
    for a in disabled:
        if a.get("email") == email and a.get("_disabled"):
            target = a
            break
    if not target:
        return JSONResponse({"error": "未在废弃列表中找到该账号"}, status_code=404)
    new_path = am.restore_from_disabled(target)
    if new_path:
        return {"ok": True, "msg": f"已恢复: {new_path.name}"}
    return JSONResponse({"ok": False, "error": "恢复失败"}, status_code=500)


# ======================================================================
# 注册相关 API
# ======================================================================

@app.get("/api/register/status")
async def register_status() -> dict:
    """获取当前注册任务状态."""
    job = rs.get_job_dict()
    # 合并历史记录
    job["history"] = rs.get_history()
    return job


@app.get("/api/register/history")
async def register_history() -> list[dict]:
    """获取历史注册记录."""
    return rs.get_history()


@app.post("/api/register/start")
async def register_start(data: dict) -> dict:
    """启动单独注册.

    Body: {"count": 10, "jobs": 3}
    """
    count = int(data.get("count", 1))
    jobs = int(data.get("jobs", 1))
    return await rs.start_registration(count, jobs)


@app.post("/api/register/stop")
async def register_stop() -> dict:
    """停止正在运行的注册任务."""
    return await rs.stop_registration()


# ---- 定时注册 ----

@app.post("/api/register/schedule")
async def register_schedule(data: dict) -> dict:
    """创建定时注册任务.

    Body: {"count": 10, "jobs": 3, "run_at": "2026-07-08T22:00:00"}
    """
    count = int(data.get("count", 1))
    jobs = int(data.get("jobs", 1))
    run_at = str(data.get("run_at", ""))
    return await rs.schedule_registration(count, jobs, run_at)


@app.get("/api/register/schedule/status")
async def register_schedule_status() -> dict:
    return rs.get_scheduled()


@app.post("/api/register/schedule/cancel")
async def register_schedule_cancel() -> dict:
    return await rs.cancel_scheduled()


@app.post("/api/register/schedule/auto-start")
async def register_schedule_auto_start(data: dict) -> dict:
    """保存定时注册的开机自启标志."""
    enabled = bool(data.get("enabled", False))
    rs.save_scheduled_auto_start(enabled)
    return {"ok": True, "auto_start_scheduled": enabled}


# ---- 均衡注册 ----

@app.get("/api/register/balanced/config")
async def balanced_config() -> dict:
    return rs.get_balanced_status()


@app.post("/api/register/balanced/config")
async def balanced_config_update(data: dict) -> dict:
    """更新均衡注册配置.

    Body: {"enabled": true, "target_available": 10, "check_interval_s": 60, "reg_count": 5, "reg_jobs": 2, "use_proxy": false}
    """
    return await rs.update_balanced_config(data)


# ======================================================================
# 生图 (AI Image Generation)
# ======================================================================

@app.get("/api/generate/accounts")
async def generate_accounts() -> list[dict]:
    """返回可用于生图的账号列表 (额度>0 + token + 钱包)."""
    return gs.get_available_accounts()


@app.post("/api/generate/chat")
async def generate_chat(data: dict) -> StreamingResponse:
    """SSE 流式生图对话.

    Body: {"email": "...", "messages": [{"role":"user","content":"..."}]}
    """
    email = str(data.get("email", ""))
    messages = list(data.get("messages", []))

    async def event_stream():
        async for event in gs.stream_chat(email, messages):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
        await gs.cleanup_after_chat(email)

    return StreamingResponse(event_stream(), media_type="text/event-stream")
