"""单次 Treza 注册流程."""

import datetime
import random
import string
import time

import httpx

from .cloudflare_email import CloudflareEmail
from .config import (
    INIT_URL,
    AUTH_URL,
    PRIVY_APP_ID,
    PRIVY_CA_ID,
    TREZA_ORIGIN,
    REQUEST_TIMEOUT,
)
from .log import DIM, GREEN, RED, YELLOW, log, kv
from .proxy_pool import ProxyPool
from .state import inc_fail, inc_ok, inc_retry, stop_event as _stop_event
from .storage import save_account

# 公共请求头
_COMMON_HEADERS = {
    "accept": "application/json",
    "accept-language": "zh-CN,zh;q=0.9",
    "cache-control": "no-cache",
    "content-type": "application/json",
    "origin": TREZA_ORIGIN,
    "referer": f"{TREZA_ORIGIN}/",
    "sec-ch-ua": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "cross-site",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
    ),
}


def _rand_prefix() -> str:
    """生成随机邮箱前缀."""
    length = random.randint(10, 16)
    chars = string.ascii_lowercase + string.digits
    return "".join(random.choices(chars, k=length))


def _init_headers() -> dict:
    return {
        **_COMMON_HEADERS,
        "pragma": "no-cache",
        "privy-app-id": PRIVY_APP_ID,
        "privy-ca-id": PRIVY_CA_ID,
        "privy-client": "react-auth:2.25.0",
    }


def _auth_headers() -> dict:
    return {
        **_COMMON_HEADERS,
        "pragma": "no-cache",
        "priority": "u=1, i",
        "privy-app-id": PRIVY_APP_ID,
        "privy-ca-id": PRIVY_CA_ID,
        "privy-client": "react-auth:2.25.0",
        "sec-fetch-storage-access": "active",
    }


async def _init_email(client: httpx.AsyncClient, email: str, tid: int) -> bool:
    """Step 1: 向 Privy 发起 passwordless init."""
    try:
        resp = await client.post(
            INIT_URL,
            headers=_init_headers(),
            json={"email": email},
        )
        data = resp.json()
        if resp.status_code == 200 and data.get("success") is True:
            log(tid, DIM, "~", f"init 成功: {email}")
            return True
        log(tid, YELLOW, "~", f"init 异常响应: {resp.status_code} {data}")
        return False
    except Exception as e:
        log(tid, RED, "✗", f"init 请求失败: {e}")
        return False


async def _authenticate(
    client: httpx.AsyncClient, email: str, code: str, tid: int
) -> dict | None:
    """Step 3: 使用验证码完成认证，返回账号数据."""
    resp = await client.post(
        AUTH_URL,
        headers=_auth_headers(),
        json={"email": email, "code": code, "mode": "login-or-sign-up"},
    )
    data = resp.json()
    if resp.status_code != 200:
        log(tid, RED, "✗", f"authenticate 失败: {resp.status_code} {data}")
        return None

    token = data.get("token")
    if not token:
        log(tid, RED, "✗", f"authenticate 无 token: {data}")
        return None
    return data


MAX_RETRIES = 3


# ---------------------------------------------------------------------------
# 单次注册入口
# ---------------------------------------------------------------------------
async def register_one(
    tid: int,
    email_backend: CloudflareEmail,
    proxy_pool: ProxyPool,
) -> dict | None:
    """执行一次完整注册流程，最多重试 MAX_RETRIES 次，成功返回 account dict，失败返回 None."""
    t_start = time.time()

    for attempt in range(1, MAX_RETRIES + 1):
        if attempt > 1:
            if inc_retry():
                log(tid, RED, "✗", f"重试次数已达总阈值 60%, 任务自动停止")
                return None
            log(tid, YELLOW, "~", f"第 {attempt}/{MAX_RETRIES} 次重试...")

        if _stop_event.is_set():
            log(tid, YELLOW, "~", "任务已停止")
            return None

        proxy_url = await proxy_pool.get_proxy()

        client_kwargs: dict = {"verify": False}
        if proxy_url:
            client_kwargs["proxy"] = proxy_url
        client_kwargs["timeout"] = httpx.Timeout(REQUEST_TIMEOUT)

        async with httpx.AsyncClient(**client_kwargs) as client:  # type: ignore[arg-type]
            try:
                # --- 创建临时邮箱 ---
                name = _rand_prefix()
                mailbox = await email_backend.create_address(client, name)
                email = mailbox["address"]
                log(tid, DIM, "+", f"邮箱: {email}")

                # --- Step 1: init ---
                ok = await _init_email(client, email, tid)
                if not ok:
                    proxy_pool.mark_bad(proxy_url)
                    log(tid, YELLOW, "~", f"init 失败 (attempt {attempt}/{MAX_RETRIES})")
                    continue

                # --- Step 2: 轮询验证码 ---
                log(tid, DIM, "~", "等待验证码邮件...")
                code, _msg = await email_backend.poll_for_code(client, email)
                log(tid, DIM, "~", f"验证码: {code}")

                # --- Step 3: authenticate ---
                data = await _authenticate(client, email, code, tid)
                if not data:
                    proxy_pool.mark_bad(proxy_url)
                    log(tid, YELLOW, "~", f"authenticate 失败 (attempt {attempt}/{MAX_RETRIES})")
                    continue

                # 代理正常 → 标记成功
                if proxy_url:
                    proxy_pool.mark_good(proxy_url)

                # --- 构造账号数据 ---
                user = data.get("user", {})
                linked = user.get("linked_accounts", [{}])
                email_info = linked[0] if linked else {}

                account = {
                    "email": email_info.get("address", email),
                    "user_id": user.get("id", ""),
                    "created_at": user.get("created_at", 0),
                    "token": data.get("token", ""),
                    "privy_access_token": data.get("privy_access_token", ""),
                    "refresh_token": data.get("refresh_token", ""),
                    "session_id": data.get("sid", ""),
                    "is_new_user": data.get("is_new_user", False),
                    "linked_accounts": linked,
                    "registered_at": datetime.datetime.utcnow().isoformat() + "Z",
                    "elapsed_s": round(time.time() - t_start, 1),
                    "proxy_used": proxy_url,
                    "retries": attempt - 1,
                }

                # --- 保存 ---
                filepath = save_account(account)
                account["_file"] = str(filepath)
                total = inc_ok()
                log(tid, GREEN, "✓", f"注册成功: {email}")
                kv(tid, "耗时", f"{account['elapsed_s']}s")
                kv(tid, "token", account["token"][:50] + "...")
                kv(tid, "已保存", str(filepath.name))
                if attempt > 1:
                    kv(tid, "重试次数", str(attempt - 1))
                log(tid, GREEN, "", f"已完成 {total}")
                return account

            except TimeoutError as e:
                log(tid, YELLOW, "~", f"超时 (attempt {attempt}/{MAX_RETRIES}): {e}")
                if proxy_url:
                    proxy_pool.mark_bad(proxy_url)
                if attempt >= MAX_RETRIES:
                    log(tid, RED, "✗", f"已达最大重试次数 {MAX_RETRIES}, 最终失败")
                    inc_fail()
                    return None
            except KeyboardInterrupt:
                return None
            except Exception as e:
                log(tid, YELLOW, "~", f"出错 (attempt {attempt}/{MAX_RETRIES}): {type(e).__name__}: {e}")
                if proxy_url:
                    proxy_pool.mark_bad(proxy_url)
                if attempt >= MAX_RETRIES:
                    log(tid, RED, "✗", f"已达最大重试次数 {MAX_RETRIES}, 最终失败")
                    inc_fail()
                    return None
            finally:
                if proxy_url:
                    proxy_pool.release(proxy_url)

    # 所有重试已耗尽
    inc_fail()
    return None
