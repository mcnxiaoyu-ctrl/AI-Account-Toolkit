"""并发调度器 — asyncio 驱动高并发注册."""

import asyncio
import signal

from .cloudflare_email import CloudflareEmail
from .log import CYAN, DIM, GREEN, RED, RESET, YELLOW, banner
from .proxy_pool import ProxyPool
from .register import register_one
from .state import counter, print_lock, stop_event
from .storage import count_accounts


def _install_signal_handlers() -> None:
    def handler(signum, frame):
        with print_lock:
            print(f"\n{YELLOW}! 收到中断信号，停止派发新任务...{RESET}")
        stop_event.set()

    signal.signal(signal.SIGINT, handler)
    try:
        signal.signal(signal.SIGTERM, handler)
    except Exception:
        pass


async def _worker(
    tid: int,
    email_backend: CloudflareEmail,
    proxy_pool: ProxyPool,
    sem: asyncio.Semaphore,
) -> dict | None:
    """带信号量控制的单任务包装."""
    async with sem:
        if stop_event.is_set():
            return None
        return await register_one(tid, email_backend, proxy_pool)


async def run(
    count: int,
    jobs: int,
    email_backend: CloudflareEmail,
    proxy_pool: ProxyPool,
) -> None:
    """主调度器."""
    _install_signal_handlers()

    # ---- 预拉取代理池 ----
    await proxy_pool.initialize(jobs)

    source_label = "Cliproxy API" if proxy_pool.source == "api" else "文件"
    pool_size = len(proxy_pool)
    banner(f"\n{CYAN}TrezaReg{RESET}  {DIM}trezalabs.com 自动注册机{RESET}")
    banner(
        f"{DIM}总数={count}  并发={jobs}  代理池={source_label}:{pool_size}条  "
        f"本地代理={proxy_pool.get_local_proxy() or '无'}  "
        f"已存账号={count_accounts()}{RESET}\n"
    )

    sem = asyncio.Semaphore(jobs)
    tasks: list[asyncio.Task] = []

    for i in range(count):
        if stop_event.is_set():
            break
        tasks.append(
            asyncio.create_task(_worker(i + 1, email_backend, proxy_pool, sem))
        )

    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    except KeyboardInterrupt:
        stop_event.set()

    canceled = sum(1 for t in tasks if t.cancelled())
    banner(
        f"\n{GREEN}完成.{RESET}  "
        f"{GREEN}成功={counter['ok']}{RESET}  "
        f"{RED}失败={counter['fail']}{RESET}  "
        f"{YELLOW}取消={canceled}{RESET}  "
        f"账号库: {DIM}accounts/{RESET}\n"
    )
