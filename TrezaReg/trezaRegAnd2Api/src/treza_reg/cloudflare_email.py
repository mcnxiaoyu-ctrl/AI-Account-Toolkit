"""Cloudflare 临时邮箱客户端 — 使用 httpx 异步 API."""

import asyncio
import hashlib
import re
import time

import httpx

from .config import (
    CF_EMAIL_ADMIN_AUTH,
    CF_EMAIL_BASE_URL,
    CF_EMAIL_DOMAIN,
    CF_EMAIL_LOGIN_EMAIL,
    CF_EMAIL_LOGIN_PASSWORD,
    POLL_TIMEOUT,
    POLL_INTERVAL,
    REQUEST_TIMEOUT,
)
from .state import stop_event


class CloudflareEmail:
    """Cloudflare 临时邮箱客户端 (admin API, 异步) — 线程安全."""

    def __init__(
        self,
        base_url: str | None = None,
        domain: str | None = None,
        login_email: str | None = None,
        login_password: str | None = None,
        timeout: int | None = None,
        verify: bool = False,
    ) -> None:
        self.base_url = (base_url or CF_EMAIL_BASE_URL).rstrip("/")
        self.domain = domain or CF_EMAIL_DOMAIN
        self.timeout = timeout or REQUEST_TIMEOUT
        self.verify = verify
        self._jwt: str | None = None
        self._login_email = login_email or CF_EMAIL_LOGIN_EMAIL
        self._login_password = login_password or CF_EMAIL_LOGIN_PASSWORD
        self._login_lock = asyncio.Lock()
        self._admin_auth = CF_EMAIL_ADMIN_AUTH

    @property
    def _base_headers(self) -> dict:
        return {"x-admin-auth": self._admin_auth, "User-Agent": "Mozilla/5.0"}

    # ------------------------------------------------------------------
    @staticmethod
    def _sha256(s: str) -> str:
        return hashlib.sha256(s.encode()).hexdigest()

    async def login(self, client: httpx.AsyncClient) -> None:
        """登录获取 JWT token (并发安全)."""
        async with self._login_lock:
            # 双重检查：可能其他协程已登录
            if self._jwt:
                return
            hashed_pw = self._sha256(self._login_password)
            resp = await client.post(
                f"{self.base_url}/user_api/login",
                headers={**self._base_headers, "Content-Type": "application/json"},
                json={"email": self._login_email, "password": hashed_pw, "cf_token": ""},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            self._jwt = data["jwt"]

    # ------------------------------------------------------------------
    async def create_address(self, client: httpx.AsyncClient, name: str) -> dict:
        """创建临时邮箱地址，返回 {id, address, jwt, address_id}."""
        if not self._jwt:
            await self.login(client)

        resp = await client.post(
            f"{self.base_url}/admin/new_address",
            headers={
                **self._base_headers,
                "x-user-token": self._jwt or "",
                "Content-Type": "application/json",
            },
            json={
                "enablePrefix": True,
                "enableRandomSubdomain": False,
                "name": name,
                "domain": self.domain,
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "id": data["address_id"],
            "address": data["address"],
            "jwt": data.get("jwt"),
            "address_id": data["address_id"],
        }

    # ------------------------------------------------------------------
    async def poll_for_code(
        self,
        client: httpx.AsyncClient,
        address: str,
        timeout: int | None = None,
        interval: int | None = None,
    ) -> tuple[str, dict]:
        """轮询邮箱直到收到 6 位数字验证码，返回 (code, msg_dict)."""
        timeout = timeout or POLL_TIMEOUT
        interval = interval or POLL_INTERVAL
        deadline = time.time() + timeout
        seen: set[str] = set()

        while time.time() < deadline:
            if stop_event.is_set():
                raise KeyboardInterrupt("用户中断")

            resp = await client.get(
                f"{self.base_url}/admin/mails",
                headers={**self._base_headers, "x-user-token": self._jwt or ""},
                params={"limit": 20, "offset": 0, "address": address},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()

            for msg in data.get("results", []):
                mid = msg.get("id", "")
                if mid in seen:
                    continue
                seen.add(mid)

                body = (
                    msg.get("raw", "")
                    or msg.get("content", "")
                    or msg.get("text", "")
                    or msg.get("html", "")
                    or ""
                )
                # 优先精确匹配 "Your code is 123456" 格式
                match = re.search(r"(?:code is|code:)\s*(\d{6})", body, re.IGNORECASE)
                if not match:
                    # 回退: 匹配独立的 6 位数字
                    match = re.search(r"\b(\d{6})\b", body)
                if match:
                    return match.group(1), msg

            await _asleep(interval)

        raise TimeoutError(f"等待验证码超时 ({timeout}s)")


async def _asleep(seconds: float) -> None:
    """可中断的异步 sleep."""
    try:
        await asyncio.sleep(seconds)
    except asyncio.CancelledError:
        pass
