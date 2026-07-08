"""线程间共享状态."""

import math
import threading

print_lock = threading.Lock()
counter: dict[str, int] = {"ok": 0, "fail": 0}
counter_lock = threading.Lock()
stop_event = threading.Event()
threshold_stopped = False  # True = 被 60% 阈值触发停止 (不再自动重试)

# 重试次数追踪 (用于 60% 阈值自动停止)
retry_count = 0
retry_limit = 0  # 由 register_service 初始化
retry_lock = threading.Lock()


def inc_ok() -> int:
    with counter_lock:
        counter["ok"] += 1
        return counter["ok"]


def inc_fail() -> int:
    with counter_lock:
        counter["fail"] += 1
        return counter["fail"]


def inc_retry() -> bool:
    """增加重试计数，返回 True 表示已超过阈值应停止."""
    global retry_count
    with retry_lock:
        retry_count += 1
        if retry_limit > 0 and retry_count >= retry_limit:
            stop_event.set()
            global threshold_stopped
            threshold_stopped = True
            return True
    return False


def reset_retry_tracker(total_count: int) -> None:
    """重置重试追踪器.

    retry_limit = ceil(total * (MAX_RETRIES-1) * 60%)  每个账号最多重试2次
    """
    global retry_count, retry_limit, threshold_stopped
    with retry_lock:
        retry_count = 0
        retry_limit = math.ceil(total_count * 2 * 0.6)
        threshold_stopped = False
