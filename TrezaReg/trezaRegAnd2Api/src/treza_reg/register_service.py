"""后台注册服务 — 支持单独/定时/均衡三种模式."""

from __future__ import annotations

import asyncio
import datetime
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .cloudflare_email import CloudflareEmail
from .config import (
    get_register_center_section,
    save_register_center_config,
)
from . import config
from .proxy_pool import ProxyPool
from .register import register_one
from .state import counter as _global_counter, stop_event, counter_lock, reset_retry_tracker, threshold_stopped

# ---- 历史记录持久化 ----
_HISTORY_FILE = Path(__file__).resolve().parent.parent.parent / "accounts" / "_reg_history.json"
_reg_history: list[dict] = []


def _load_history() -> list[dict]:
    global _reg_history
    try:
        if _HISTORY_FILE.exists():
            _reg_history = json.loads(_HISTORY_FILE.read_text("utf-8"))
    except Exception:
        _reg_history = []
    return _reg_history


def _save_history() -> None:
    try:
        _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        # 只保留最近 50 条记录
        _HISTORY_FILE.write_text(json.dumps(_reg_history[-50:], ensure_ascii=False, indent=2), "utf-8")
    except Exception:
        pass


def get_history() -> list[dict]:
    return _reg_history


# 启动时加载
_load_history()


# ======================================================================
# 注册任务记录
# ======================================================================

@dataclass
class RegTaskResult:
    tid: int
    email: str = ""
    status: str = "pending"  # pending | running | ok | fail
    elapsed_s: float = 0
    error: str = ""
    user_id: str = ""
    token_preview: str = ""
    started_at: str = ""
    finished_at: str = ""


# ======================================================================
# 注册 Job (单次)
# ======================================================================

@dataclass
class RegistrationJob:
    count: int = 0
    jobs: int = 1

    # 运行时状态
    status: str = "idle"  # idle | running | stopping | done
    total: int = 0
    completed: int = 0
    success: int = 0
    fail: int = 0
    started_at: float = 0
    finished_at: float = 0
    message: str = ""
    tasks: list[RegTaskResult] = field(default_factory=list)
    _stop_requested: bool = False

    def to_dict(self) -> dict:
        elapsed = 0.0
        if self.started_at > 0 and self.status == "running":
            elapsed = time.time() - self.started_at
        eta = 0.0
        if self.completed > 0 and self.completed < self.total:
            avg = elapsed / self.completed
            eta = avg * (self.total - self.completed)

        return {
            "status": self.status,
            "total": self.total,
            "completed": self.completed,
            "success": self.success,
            "fail": self.fail,
            "percent": round(self.completed / max(self.total, 1) * 100, 1),
            "success_rate": round(self.success / max(self.completed, 1) * 100, 1) if self.completed > 0 else 0,
            "elapsed_s": round(elapsed, 1),
            "eta_s": round(eta, 1),
            "message": self.message,
            "tasks": [
                {
                    "tid": t.tid,
                    "email": t.email,
                    "status": t.status,
                    "elapsed_s": t.elapsed_s,
                    "error": t.error,
                    "user_id": t.user_id,
                    "token_preview": t.token_preview,
                    "started_at": t.started_at,
                    "finished_at": t.finished_at,
                }
                for t in self.tasks[-100:]  # 最多返回最近 100 条
            ],
        }


# ======================================================================
# 全局注册服务 (单例)
# ======================================================================

_current_job = RegistrationJob()
_job_lock = asyncio.Lock()
_bg_task: asyncio.Task | None = None

# 均衡注册配置
_balanced_config: dict[str, Any] = {
    "enabled": False,
    "target_available": 10,      # 目标可用账号数
    "check_interval_s": 60,      # 检查间隔 (秒)
    "reg_count": 5,              # 每次补注册数量
    "reg_jobs": 2,               # 补注册并发
}
# 从 config.yaml 加载持久化配置覆盖默认值
_saved_balanced = get_register_center_section("balanced")
if _saved_balanced:
    _balanced_config.update(_saved_balanced)
_balanced_task: asyncio.Task | None = None


# ======================================================================
# 获取/重置 Job
# ======================================================================

def get_job() -> RegistrationJob:
    return _current_job


def get_job_dict() -> dict:
    return _current_job.to_dict()


# ======================================================================
# 单独注册: 启动
# ======================================================================

async def start_registration(count: int, jobs: int) -> dict:
    global _current_job, _bg_task

    async with _job_lock:
        if _current_job.status == "running":
            return {"ok": False, "error": "注册任务已在运行中"}

        # 重置
        _current_job = RegistrationJob(
            count=count,
            jobs=jobs,
            status="running",
            total=count,
            started_at=time.time(),
        )
        stop_event.clear()
        # 重置全局计数器
        with counter_lock:
            _global_counter["ok"] = 0
            _global_counter["fail"] = 0
        # 初始化重试阈值 (总数 * 每个账号最多2次重试 * 60%)
        reset_retry_tracker(count)

    _bg_task = asyncio.create_task(_run_registration())
    return {"ok": True, "msg": f"注册任务已启动: 总数={count} 并发={jobs}"}


async def _run_registration() -> None:
    """后台执行注册 (在 asyncio 事件循环中运行)."""
    global _current_job

    job = _current_job
    jobs = max(1, min(job.jobs, job.count))

    # 构建邮箱和代理 (代理配置全部从 config.yaml 读取)
    email_backend = CloudflareEmail(verify=False)
    proxy_pool = ProxyPool(jobs_hint=jobs)
    await proxy_pool.initialize(jobs)

    # 创建任务列表
    sem = asyncio.Semaphore(jobs)
    task_list: list[RegTaskResult] = []
    # 补全任务记录
    for i in range(job.total):
        tr = RegTaskResult(tid=i + 1)
        task_list.append(tr)
    job.tasks = task_list

    async def _worker_wrapper(tr: RegTaskResult) -> None:
        if stop_event.is_set():
            tr.status = "fail"
            tr.error = "已停止"
            return
        async with sem:
            tr.status = "running"
            tr.started_at = datetime.datetime.now().strftime("%H:%M:%S")
            t0 = time.time()
            try:
                result = await register_one(tr.tid, email_backend, proxy_pool)
                tr.elapsed_s = round(time.time() - t0, 1)
                tr.finished_at = datetime.datetime.now().strftime("%H:%M:%S")
                if result:
                    tr.status = "ok"
                    tr.email = result.get("email", "")
                    tr.user_id = result.get("user_id", "")
                    tr.token_preview = result.get("token", "")[:30] + "..."
                    job.success += 1
                    # 注册成功后立即查询余额
                    try:
                        from .account_manager import check_and_update_account
                        proxy_url = None
                        if hasattr(proxy_pool, '_current_proxies'):
                            pass
                        await check_and_update_account(result, proxy_url)
                    except Exception:
                        pass
                else:
                    tr.status = "fail"
                    tr.error = "注册失败"
                    job.fail += 1
            except Exception as exc:
                tr.status = "fail"
                tr.error = str(exc)[:100]
                tr.elapsed_s = round(time.time() - t0, 1)
                tr.finished_at = datetime.datetime.now().strftime("%H:%M:%S")
                job.fail += 1
            finally:
                job.completed += 1

    try:
        tasks = [asyncio.create_task(_worker_wrapper(tr)) for tr in task_list]
        await asyncio.gather(*tasks, return_exceptions=True)
    except Exception:
        pass

    job.status = "done"
    job.finished_at = time.time()
    if threshold_stopped:
        job.message = f"重试达60%阈值自动停止: 成功 {job.success}, 失败 {job.fail}"
    else:
        job.message = f"完成: 成功 {job.success}, 失败 {job.fail}"

    # 保存到历史记录
    _reg_history.append({
        "started_at": datetime.datetime.fromtimestamp(job.started_at).isoformat() if job.started_at else "",
        "finished_at": datetime.datetime.fromtimestamp(job.finished_at).isoformat() if job.finished_at else "",
        "count": job.count,
        "jobs": job.jobs,
        "success": job.success,
        "fail": job.fail,
        "message": job.message,
        "tasks": [{
            "tid": t.tid,
            "email": t.email,
            "status": t.status,
            "elapsed_s": t.elapsed_s,
            "error": t.error,
            "user_id": t.user_id,
            "token_preview": t.token_preview,
            "started_at": t.started_at,
            "finished_at": t.finished_at,
        } for t in job.tasks],
    })
    _save_history()


# ======================================================================
# 停止注册
# ======================================================================

async def stop_registration() -> dict:
    global _current_job
    async with _job_lock:
        if _current_job.status != "running":
            return {"ok": False, "error": "没有运行中的注册任务"}
        _current_job.status = "stopping"
    stop_event.set()
    _current_job.message = "用户停止"
    _current_job.finished_at = time.time()
    return {"ok": True, "msg": "正在停止..."}


# ======================================================================
# 定时注册
# ======================================================================

_scheduled: dict[str, Any] = {}


async def schedule_registration(
    count: int,
    jobs: int,
    run_at: str,  # ISO datetime string e.g. "2026-07-08T22:00:00"
    auto_start: bool | None = None,
) -> dict:
    global _scheduled, _bg_task

    try:
        target_time = datetime.datetime.fromisoformat(run_at)
    except ValueError:
        return {"ok": False, "error": "时间格式错误，请使用 ISO 格式 (YYYY-MM-DDTHH:MM:SS)"}

    now = datetime.datetime.now()
    delay = (target_time - now).total_seconds()
    if delay <= 0:
        return {"ok": False, "error": "目标时间已过"}

    _scheduled = {
        "active": True,
        "count": count,
        "jobs": jobs,
        "run_at": run_at,
        "delay_s": delay,
    }

    # 持久化 count/jobs 到 config.yaml
    save_register_center_config("scheduled", {"count": count, "jobs": jobs})
    if auto_start is not None:
        save_register_center_config("auto_start_scheduled", auto_start)

    async def _delayed_start():
        await asyncio.sleep(delay)
        await start_registration(count, jobs)
        _scheduled["active"] = False

    _bg_task = asyncio.create_task(_delayed_start())
    return {"ok": True, "msg": f"已计划 {count} 个账号于 {run_at} 开始注册 (等待 {delay:.0f}s)"}


def get_scheduled() -> dict:
    if not _scheduled.get("active"):
        return {"active": False, "auto_start_scheduled": config.REG_CENTER_AUTO_START_SCHEDULED}
    remaining = 0
    try:
        target = datetime.datetime.fromisoformat(_scheduled["run_at"])
        remaining = (target - datetime.datetime.now()).total_seconds()
    except Exception:
        pass
    return {**_scheduled, "remaining_s": max(0, int(remaining)), "auto_start_scheduled": config.REG_CENTER_AUTO_START_SCHEDULED}


async def cancel_scheduled() -> dict:
    global _scheduled, _bg_task
    if not _scheduled.get("active"):
        return {"ok": False, "error": "没有定时任务"}
    if _bg_task:
        _bg_task.cancel()
    _scheduled = {}
    # 持久化: 清除定时任务配置, 关闭开机自启
    save_register_center_config("scheduled", {})
    save_register_center_config("auto_start_scheduled", False)
    return {"ok": True, "msg": "定时任务已取消"}


# ======================================================================
# 均衡注册
# ======================================================================

def get_balanced_config() -> dict:
    return dict(_balanced_config)


def get_balanced_status() -> dict:
    from .account_manager import get_stats as _am_stats
    stats = _am_stats()
    cfg = get_balanced_config()
    needed = max(0, cfg["target_available"] - stats["available"])
    return {
        **cfg,
        "auto_start_balanced": config.REG_CENTER_AUTO_START_BALANCED,
        "current_available": stats["available"],
        "current_total": stats["total"],
        "needed": needed,
        "will_register": needed > 0,
    }


async def update_balanced_config(config: dict) -> dict:
    global _balanced_config, _balanced_task

    # 分离 auto_start 标志 (需保存到 register_center 顶级)
    auto_start = config.pop("auto_start_balanced", None)
    if auto_start is not None:
        save_register_center_config("auto_start_balanced", auto_start)

    _balanced_config.update(config)
    # 持久化到 config.yaml
    save_register_center_config("balanced", _balanced_config)

    # 重启后台监控任务
    if _balanced_task:
        _balanced_task.cancel()

    if _balanced_config.get("enabled"):
        _balanced_task = asyncio.create_task(_balanced_monitor())

    return {"ok": True, "config": get_balanced_config()}


async def _balanced_monitor() -> None:
    """后台定期检查账号池，低于阈值自动补注册."""
    from .account_manager import get_stats
    from .state import stop_event as _stop

    while _balanced_config.get("enabled"):
        try:
            stats = get_stats()
            need = max(0, _balanced_config["target_available"] - stats["available"])

            if need > 0 and _current_job.status != "running" and not threshold_stopped:
                reg_count = min(need, _balanced_config.get("reg_count", need))
                reg_jobs = min(reg_count, _balanced_config.get("reg_jobs", 1))
                await start_registration(
                    count=reg_count,
                    jobs=reg_jobs,
                )
                # 等待当前注册批次完成再检查
                while _current_job.status == "running":
                    await asyncio.sleep(5)
                # 阈值停止则退出均衡循环 (持久化到 config.yaml)
                if threshold_stopped:
                    _balanced_config["enabled"] = False
                    save_register_center_config("balanced", _balanced_config)
                    break

        except Exception:
            pass

        await asyncio.sleep(_balanced_config.get("check_interval_s", 60))


# ======================================================================
# 开机自启动
# ======================================================================

def save_scheduled_auto_start(enabled: bool) -> None:
    """独立保存定时注册的开机自启标志到 config.yaml."""
    save_register_center_config("auto_start_scheduled", enabled)


async def init_auto_start() -> None:
    """根据 config.yaml 中的 auto_start 标志恢复均衡/定时注册任务."""
    if config.REG_CENTER_AUTO_START_BALANCED and _balanced_config.get("enabled"):
        global _balanced_task
        _balanced_task = asyncio.create_task(_balanced_monitor())

    if config.REG_CENTER_AUTO_START_SCHEDULED:
        sc = get_register_center_section("scheduled")
        count = sc.get("count", 10)
        jobs = sc.get("jobs", 2)
        run_at = (datetime.datetime.now() + datetime.timedelta(minutes=10)).isoformat()
        await schedule_registration(count, jobs, run_at)
