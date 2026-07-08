"""代理池管理 — 支持 API 动态获取 + 连通性测试 + 冷却管理."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from .config import (
    PROXY_ENABLED,
    PROXY_API_URL,
    PROXY_TEST_URLS,
    PROXY_TEST_TIMEOUT,
)
from .log import DIM, RED, RESET, YELLOW


def _parse_proxy_line(line: str) -> str | None:
    """解析 host:port 行为 http://host:port."""
    line = line.strip()
    if not line or ":" not in line:
        return None
    if "://" in line:
        return line
    return f"http://{line}"


def _mask(s: str) -> str:
    """脱敏显示代理地址."""
    if "://" in s:
        scheme, rest = s.split("://", 1)
        if "@" in rest:
            return f"{scheme}://***@{rest.split('@', 1)[1]}"
        return s
    return s


class ProxyPool:
    """代理池 — 从 API URL 动态获取 + 连通性测试 + 冷却管理.

    使用方式:
        pool = ProxyPool(jobs_hint=3)
        await pool.initialize()

        proxy = await pool.get_proxy()   # 获取可用代理
        ... 使用代理 ...
        pool.mark_good(proxy)            # 标记成功
        pool.release(proxy)              # 释放
    """

    def __init__(self, jobs_hint: int = 1) -> None:
        self._enabled: bool = PROXY_ENABLED and bool(PROXY_API_URL)
        self._api_url: str = PROXY_API_URL
        self._test_urls: list[str] = list(PROXY_TEST_URLS)
        self._test_timeout: int = PROXY_TEST_TIMEOUT
        self._fetch_count: int = max(1, jobs_hint * 2)

        # 运行时状态
        self._pool: list[str] = []
        self._states: dict[str, dict[str, Any]] = {}
        self._active: set[str] = set()
        self._lock = asyncio.Lock()
        self._last_refresh: float = 0.0

    @property
    def enabled(self) -> bool:
        return self._enabled

    def __len__(self) -> int:
        return len(self._pool)

    # ------------------------------------------------------------------
    # initialize
    # ------------------------------------------------------------------

    async def initialize(self, jobs: int = 1) -> None:
        """预拉取代理."""
        if not self._enabled:
            return
        self._fetch_count = max(1, jobs * 2)
        await self._fetch_from_api()
        print(f"  {DIM}代理池: 获取 {len(self._pool)} 条可用代理{RESET}")

    # ------------------------------------------------------------------
    # get_proxy
    # ------------------------------------------------------------------

    async def get_proxy(self) -> str | None:
        """获取一个可用代理.

        优先从池中选择 (非冷却、非占用)。
        池为空时自动从 API 刷新。
        """
        if not self._enabled:
            return None

        # 检查是否需要刷新
        need_refresh = False
        async with self._lock:
            available = sum(
                1 for p in self._pool
                if p not in self._active and not self._is_cooling(p)
            )
            need_refresh = (
                len(self._pool) == 0
                or available == 0
                or (time.time() - self._last_refresh) > 30
            )
        if need_refresh:
            await self._fetch_from_api()

        # 选代理
        async with self._lock:
            if self._pool:
                available = [
                    p for p in self._pool
                    if p not in self._active and not self._is_cooling(p)
                ]
                if not available:
                    available = sorted(
                        self._pool,
                        key=lambda p: self._states.get(p, {}).get("fail_count", 0),
                    )
                selected = available[0]
                self._active.add(selected)
                return selected

        return None

    # ------------------------------------------------------------------
    # release / mark
    # ------------------------------------------------------------------

    def release(self, proxy: str) -> None:
        if proxy:
            self._active.discard(proxy)

    def mark_good(self, proxy: str) -> None:
        if not proxy:
            return
        s = self._states.setdefault(proxy, {"fail_count": 0, "cooldown_until": 0.0})
        s["fail_count"] = 0

    def mark_bad(self, proxy: str) -> None:
        if not proxy:
            return
        s = self._states.setdefault(proxy, {"fail_count": 0, "cooldown_until": 0.0})
        s["fail_count"] += 1
        if s["fail_count"] >= 1:
            s["cooldown_until"] = time.time() + 300

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    async def _fetch_from_api(self) -> None:
        """从 API URL 拉取代理并测试连通性."""
        try:
            req_count = max(1, self._fetch_count)
            url = self._api_url
            # 支持 URL 中已有 num 参数，也支持没有的
            if "num=" not in url and "?" in url:
                url = f"{url}&num={req_count}"
            elif "num=" not in url:
                url = f"{url}?num={req_count}"

            async with httpx.AsyncClient(verify=False, timeout=httpx.Timeout(8)) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                raw: list[str] = []
                for line in resp.text.strip().splitlines():
                    parsed = _parse_proxy_line(line)
                    if parsed:
                        raw.append(parsed)

                if not raw:
                    print(f"  {YELLOW}API 返回空{RESET}")
                    return

                # 连通性测试
                working: list[str] = []
                for proxy_url in raw:
                    ok = await self._test_proxy(proxy_url)
                    if ok:
                        working.append(proxy_url)

                if not working:
                    print(f"  {RED}所有代理测试失败 ({len(raw)} 条){RESET}")
                    return

                async with self._lock:
                    for p in working:
                        if p not in self._pool:
                            self._pool.append(p)
                    self._last_refresh = time.time()

                print(f"  {DIM}代理API: {len(raw)} → 可用 {len(working)} | 池 {len(self._pool)}{RESET}")
        except Exception as exc:
            print(f"  {YELLOW}代理API请求失败: {exc}{RESET}")

    async def _test_proxy(self, proxy_url: str) -> bool:
        """穿过代理访问测试 URL."""
        if not self._test_urls:
            return True

        try:
            async with httpx.AsyncClient(
                proxy=proxy_url,
                verify=False,
                timeout=httpx.Timeout(self._test_timeout),
            ) as client:
                for url in self._test_urls:
                    resp = await client.get(url)
                    if resp.status_code >= 500:
                        return False
        except Exception:
            return False
        return True

    def _is_cooling(self, proxy: str) -> bool:
        s = self._states.get(proxy)
        if not s:
            return False
        if time.time() < s.get("cooldown_until", 0):
            return True
        s["cooldown_until"] = 0.0
        s["fail_count"] = 0
        return False
