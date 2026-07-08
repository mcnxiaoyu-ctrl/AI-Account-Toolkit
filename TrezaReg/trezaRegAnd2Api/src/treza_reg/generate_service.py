"""Treza AI 生图服务 — Chat API 代理."""

from __future__ import annotations

import json
from typing import AsyncGenerator

import httpx

from . import account_manager as am
from .config import TREZA_ORIGIN


async def cleanup_after_chat(email: str) -> None:
    """对话后检查账号额度，额度<=0则自动废弃."""
    try:
        accounts = am.load_accounts(include_disabled=False)
        target = None
        for a in accounts:
            if a.get("email") == email:
                target = a
                break
        if target:
            await am.check_and_update_account(target, proxy_url=None)
    except Exception:
        pass


def get_available_accounts() -> list[dict]:
    """返回可用于生图的账号列表 (credits > 0, 有 token 和钱包地址)."""
    accounts = am.load_accounts(include_disabled=False)
    result: list[dict] = []
    for a in accounts:
        if a.get("_disabled"):
            continue
        credits = a.get("credits")
        token = a.get("token", "")
        wallet = a.get("wallet_address", "")
        if credits is not None and credits > 0 and token and wallet:
            result.append({
                "email": a.get("email", ""),
                "wallet_address": wallet,
                "credits": credits,
            })
    return result


async def stream_chat(
    account_email: str, messages: list[dict],
) -> AsyncGenerator[dict, None]:
    """向 Treza Chat API 发起流式请求，逐条 yield 解析后的 SSE 事件.

    401 时自动用 refresh_token 刷新 session 并重试一次.
    """
    accounts = am.load_accounts(include_disabled=False)
    target = None
    for a in accounts:
        if a.get("email") == account_email:
            target = a
            break

    if not target:
        yield {"error": "账号不存在"}
        return

    token = target.get("token", "")
    wallet = target.get("wallet_address", "")
    refresh_token = target.get("refresh_token", "")

    if not token or not wallet:
        yield {"error": "账号缺少 token 或钱包地址"}
        return

    url = f"{TREZA_ORIGIN}/api/chat"
    body = {"identifier": wallet, "messages": messages}
    print(f"[chat] POST {url}")
    print(f"[chat] account: {account_email}  wallet: {wallet[:20]}...")
    print(f"[chat] messages ({len(messages)}): {json.dumps(messages, ensure_ascii=False)[:500]}")

    def _headers(tok: str) -> dict:
        return {
            "Authorization": f"Bearer {tok}",
            "Content-Type": "application/json",
            "accept": "text/event-stream",
            "origin": TREZA_ORIGIN,
            "referer": f"{TREZA_ORIGIN}/",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
            ),
        }

    for attempt in range(2):
        async with httpx.AsyncClient(verify=False, timeout=httpx.Timeout(120)) as client:
            async with client.stream("POST", url, headers=_headers(token), json=body) as response:
                print(f"[chat] response status: {response.status_code} (attempt {attempt+1})")

                # 401 → 刷新 token 后重试
                if response.status_code == 401 and attempt == 0 and refresh_token:
                    await response.aread()
                    print(f"[chat] 401, 尝试刷新 session...")
                    new_data = await am.refresh_session(token, refresh_token)
                    if new_data and new_data.get("token"):
                        token = new_data["token"]
                        target["token"] = token
                        new_rt = new_data.get("refresh_token", "")
                        if new_rt:
                            target["refresh_token"] = new_rt
                            refresh_token = new_rt
                        am.save_account(target)
                        print(f"[chat] token 已刷新, 重试请求...")
                        continue
                    else:
                        print(f"[chat] session 刷新失败")

                # 非 200 → 返回错误
                if response.status_code != 200:
                    try:
                        err_body = (await response.aread()).decode()[:300]
                    except Exception:
                        err_body = ""
                    yield {"error": f"Chat API 返回 {response.status_code}: {err_body}"}
                    return

                # 正常流式读取
                async for line in response.aiter_lines():
                    if line:
                        print(f"[chat] <- {line[:200]}")
                    if line.startswith("data: "):
                        data_str = line[6:]
                        if data_str.strip() == "[DONE]":
                            return
                        try:
                            yield json.loads(data_str)
                        except json.JSONDecodeError:
                            pass
                return
