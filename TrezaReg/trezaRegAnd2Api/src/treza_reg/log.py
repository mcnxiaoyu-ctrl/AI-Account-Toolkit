"""ANSI 颜色码和日志函数."""

import datetime
from .state import print_lock

# ANSI 颜色
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
WHITE = "\033[97m"
DIM = "\033[2m"
RESET = "\033[0m"


def _ts() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S")


def log(tid: int, color: str, symbol: str, msg: str) -> None:
    with print_lock:
        print(f"{DIM}[{_ts()}][#{tid:03d}]{RESET} {color}{symbol} {msg}{RESET}")


def kv(tid: int, key: str, val: str) -> None:
    with print_lock:
        print(f"           {DIM}{key:<16}{RESET} {YELLOW}{val}{RESET}")


def banner(text: str) -> None:
    with print_lock:
        print(text)
