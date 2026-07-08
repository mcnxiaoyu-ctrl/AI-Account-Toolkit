"""账号存储 — 每个账号存为独立 JSON 文件到 accounts 目录.

注册流程使用 save_account() 保存新注册账号。
Web 面板使用 account_manager 进行额度查询和废弃管理。
"""

import json
import datetime
from pathlib import Path

from .config import get_accounts_dir


def save_account(account: dict) -> Path:
    """将单条账号保存为独立 JSON 文件.

    文件名格式: {email前缀}_{timestamp}.json
    保持与 account_manager.save_account 的兼容性。
    """
    from .account_manager import save_account as _am_save
    return _am_save(account)


def load_all_accounts() -> list[dict]:
    """加载 accounts 目录下所有账号 JSON (仅活跃)."""
    from .account_manager import load_accounts
    return load_accounts(include_disabled=False)


def count_accounts() -> int:
    """统计活跃账号数量."""
    from .account_manager import get_stats
    return get_stats()["total"]
