"""账号管理 — 加载/更新/额度查询/废弃迁移."""

from __future__ import annotations

import json
import shutil
import datetime
from pathlib import Path
from typing import Any

import httpx

from .config import (
    _ROOT,
    ACCOUNTS_DIR,
    PRIVY_APP_ID,
    PRIVY_CA_ID,
    TREZA_ORIGIN,
    REQUEST_TIMEOUT,
)

DISABLED_DIR_NAME = "accounts_disabled"


# ---------------------------------------------------------------------------
# 路径工具
# ---------------------------------------------------------------------------

def _get_accounts_dir() -> Path:
    p = Path(ACCOUNTS_DIR)
    if not p.is_absolute():
        p = _ROOT / ACCOUNTS_DIR
    p.mkdir(parents=True, exist_ok=True)
    return p


def _get_disabled_dir() -> Path:
    p = Path(DISABLED_DIR_NAME)
    if not p.is_absolute():
        p = _ROOT / DISABLED_DIR_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# 账号 CRUD
# ---------------------------------------------------------------------------

def load_accounts(*, include_disabled: bool = False) -> list[dict[str, Any]]:
    """加载所有账号 JSON 文件."""
    result: list[dict[str, Any]] = []
    dirs = [_get_accounts_dir()]
    if include_disabled:
        dirs.append(_get_disabled_dir())
    for d in dirs:
        if not d.exists():
            continue
        for f in sorted(d.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                data["_file"] = str(f)
                data["_disabled"] = (d.name == DISABLED_DIR_NAME)
                result.append(data)
            except Exception:
                pass
    return result


def get_account_file(email: str) -> Path | None:
    """根据邮箱名查找账号文件."""
    for f in _get_accounts_dir().glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if data.get("email") == email:
                return f
        except Exception:
            pass
    return None


def save_account(account: dict[str, Any]) -> Path:
    """保存/更新账号 JSON 文件."""
    filepath = account.get("_file")
    if filepath:
        filepath = Path(filepath)
    else:
        email = account.get("email", "unknown")
        prefix = email.split("@")[0][:40]
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filepath = _get_accounts_dir() / f"{prefix}_{ts}.json"

    # 清理内部字段
    clean = {k: v for k, v in account.items() if not k.startswith("_")}
    filepath.write_text(json.dumps(clean, indent=2, ensure_ascii=False), encoding="utf-8")
    return filepath


def move_to_disabled(account: dict[str, Any]) -> Path | None:
    """将账号移至废弃文件夹."""
    src = account.get("_file")
    if not src:
        return None
    src = Path(src)
    if not src.exists():
        return None
    dst = _get_disabled_dir() / src.name
    # 如果目标已存在，加时间戳
    if dst.exists():
        ts = datetime.datetime.now().strftime("%H%M%S")
        dst = _get_disabled_dir() / f"{src.stem}_{ts}{src.suffix}"
    shutil.move(str(src), str(dst))
    return dst


def restore_from_disabled(account: dict[str, Any]) -> Path | None:
    """从废弃文件夹恢复到正常账号库."""
    src = account.get("_file")
    if not src:
        return None
    src = Path(src)
    if not src.exists():
        return None
    dst = _get_accounts_dir() / src.name
    if dst.exists():
        ts = datetime.datetime.now().strftime("%H%M%S")
        dst = _get_accounts_dir() / f"{src.stem}_{ts}{src.suffix}"
    shutil.move(str(src), str(dst))
    return dst


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------

def get_stats() -> dict[str, int]:
    """返回账号统计."""
    active = load_accounts(include_disabled=False)
    disabled = load_accounts(include_disabled=True)
    disabled = [a for a in disabled if a.get("_disabled")]

    available = sum(
        1 for a in active
        if a.get("credits", 0) > 0 or a.get("credits") is None
    )
    unavailable = sum(
        1 for a in active
        if a.get("credits", 0) <= 0 and a.get("credits") is not None
    )

    return {
        "total": len(active),
        "available": available,
        "disabled": len(disabled) + unavailable,
    }


# ---------------------------------------------------------------------------
# Privy API: 获取用户关联钱包
# ---------------------------------------------------------------------------

async def fetch_wallet_from_privy(
    privy_access_token: str,
    proxy_url: str | None = None,
) -> str | None:
    """通过 Privy wallets API 获取用户钱包地址."""
    if not privy_access_token:
        print("  [wallet] 跳过: 无 privy_access_token")
        return None

    url = "https://auth.privy.io/api/v1/wallets"
    headers = {
        "Authorization": f"Bearer {privy_access_token}",
        "privy-app-id": PRIVY_APP_ID,
        "privy-ca-id": PRIVY_CA_ID,
        "privy-client": "react-auth:2.25.0",
        "Content-Type": "application/json",
        "origin": TREZA_ORIGIN,
        "referer": f"{TREZA_ORIGIN}/",
        "accept": "application/json",
    }
    client_kwargs: dict[str, Any] = {"verify": False, "timeout": httpx.Timeout(REQUEST_TIMEOUT)}
    if proxy_url:
        client_kwargs["proxy"] = proxy_url

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:  # type: ignore[arg-type]
            resp = await client.post(url, headers=headers, json={"chain_type": "ethereum"})
            print(f"  [wallet] POST {url} -> {resp.status_code}")
            resp.raise_for_status()
            data = resp.json()
            addr = data.get("address", "")
            print(f"  [wallet] 获取到: {addr}")
            return addr if addr else None
    except Exception as e:
        print(f"  [wallet] 失败: {type(e).__name__}: {e}")

    return None


# ---------------------------------------------------------------------------
# Privy Session 刷新
# ---------------------------------------------------------------------------

async def refresh_session(
    token: str,
    refresh_token: str,
    proxy_url: str | None = None,
) -> dict[str, Any] | None:
    """使用 refresh_token 刷新 Privy session，返回新的 token 数据."""
    if not token or not refresh_token:
        print("  [refresh] 跳过: 无 token 或 refresh_token")
        return None

    url = "https://auth.privy.io/api/v1/sessions"
    headers = {
        "Authorization": f"Bearer {token}",
        "privy-app-id": PRIVY_APP_ID,
        "privy-ca-id": PRIVY_CA_ID,
        "privy-client": "react-auth:2.25.0",
        "Content-Type": "application/json",
        "origin": TREZA_ORIGIN,
        "referer": f"{TREZA_ORIGIN}/",
        "accept": "application/json",
    }
    client_kwargs: dict[str, Any] = {"verify": False, "timeout": httpx.Timeout(REQUEST_TIMEOUT)}
    if proxy_url:
        client_kwargs["proxy"] = proxy_url

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:  # type: ignore[arg-type]
            resp = await client.post(url, headers=headers, json={"refresh_token": refresh_token})
            print(f"  [refresh] POST {url} -> {resp.status_code}")
            if resp.status_code != 200:
                print(f"  [refresh] 失败: {resp.text[:200]}")
                return None
            data = resp.json()
            new_token = data.get("token", "")
            print(f"  [refresh] 成功, 新token: {new_token[:30]}..." if new_token else "  [refresh] 响应无token")
            return data
    except Exception as e:
        print(f"  [refresh] 异常: {type(e).__name__}: {e}")
        return None


# ---------------------------------------------------------------------------
# Treza 额度查询
# ---------------------------------------------------------------------------

async def fetch_credits(
    token: str,
    identifier: str,
    proxy_url: str | None = None,
) -> dict[str, Any] | None:
    """查询 Treza 账号额度.

    GET https://www.trezalabs.com/api/billing/credits?identifier={wallet}
    Authorization: Bearer {token}
    """
    if not token or not identifier:
        return None

    url = f"{TREZA_ORIGIN}/api/billing/credits"
    headers = {
        "Authorization": f"Bearer {token}",
        "accept": "*/*",
        "accept-language": "zh-CN,zh;q=0.9",
        "cache-control": "no-cache",
        "origin": TREZA_ORIGIN,
        "referer": f"{TREZA_ORIGIN}/",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
        ),
    }
    client_kwargs: dict[str, Any] = {"verify": False, "timeout": httpx.Timeout(REQUEST_TIMEOUT)}
    if proxy_url:
        client_kwargs["proxy"] = proxy_url

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:  # type: ignore[arg-type]
            resp = await client.get(url, headers=headers, params={"identifier": identifier})
            print(f"  [credits] GET {url}?identifier={identifier[:10]}... -> {resp.status_code}")
            resp.raise_for_status()
            data = resp.json()
            credits = data.get("balanceUsd") if isinstance(data, dict) else data
            print(f"  [credits] 额度: {credits}")
            return data
    except Exception as e:
        print(f"  [credits] 失败: {type(e).__name__}: {e}")
        return None


# ---------------------------------------------------------------------------
# 综合检查 & 更新
# ---------------------------------------------------------------------------

async def check_and_update_account(
    account: dict[str, Any],
    proxy_url: str | None = None,
) -> dict[str, Any]:
    """对单个账号进行 钱包获取 → 额度查询 → 更新文件 → 额度<=0则迁移.

    返回更新后的 account dict.
    """
    email = account.get("email", "?")
    print(f"[查额] {email}")

    token = account.get("token", "") or account.get("privy_access_token", "")
    wallet = account.get("wallet_address", "") or account.get("identifier", "")
    print(f"  token: {'有' if token else '无'} ({token[:30]}...)" if token else "  token: 无")
    print(f"  wallet: {wallet or '无'}")

    # 1. 无钱包则尝试从 Privy 获取
    if not wallet:
        privy_token = account.get("privy_access_token", "")
        print(f"  privy_access_token: {'有' if privy_token else '无'}")
        wallet = await fetch_wallet_from_privy(privy_token, proxy_url)
        if wallet:
            account["wallet_address"] = wallet
        else:
            print(f"  [查额] 未获取到钱包, 跳过额度查询")

    # 2. 有 token + wallet 则查询额度
    if token and wallet:
        result = await fetch_credits(token, wallet, proxy_url)
        if result:
            credits = result.get("balanceUsd") if isinstance(result, dict) else result
            if isinstance(credits, (int, float)):
                account["credits"] = credits
                account["credits_checked_at"] = datetime.datetime.utcnow().isoformat() + "Z"
                account["credits_raw"] = result

    # 3. 保存更新
    new_path = save_account(account)
    account["_file"] = str(new_path)

    # 4. 额度 <= 0 → 移至废弃
    credits_val = account.get("credits")
    if credits_val is not None and credits_val <= 0:
        new_disabled = move_to_disabled(account)
        if new_disabled:
            account["_disabled"] = True
            account["_file"] = str(new_disabled)

    print(f"[查额] {email} 完成: credits={account.get('credits')}, wallet={account.get('wallet_address', '')[:12]}...")
    return account


async def check_all_accounts(
    proxy_url: str | None = None,
    progress_cb=None,
) -> tuple[int, int]:
    """对所有活跃账号发起额度检查.

    progress_cb(current, total, email, status) 可选进度回调.
    返回 (checked_count, disabled_count).
    """
    accounts = load_accounts(include_disabled=False)
    checked = 0
    moved = 0

    for i, acct in enumerate(accounts):
        try:
            updated = await check_and_update_account(acct, proxy_url)
            checked += 1
            if updated.get("_disabled"):
                moved += 1
            if progress_cb:
                progress_cb(i + 1, len(accounts), acct.get("email", ""), "ok")
        except Exception as exc:
            if progress_cb:
                progress_cb(i + 1, len(accounts), acct.get("email", ""), str(exc))

    return checked, moved
