"""TrezaReg 入口 — CLI 参数解析 + 启动."""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from .cloudflare_email import CloudflareEmail
from .config import _ROOT
from .proxy_pool import ProxyPool
from .runner import run as run_register


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="treza-reg",
        description="TrezaReg - Treza 自动注册机 (Cloudflare 临时邮箱 + Cliproxy 代理池)",
    )
    sub = parser.add_subparsers(dest="command", help="子命令")

    # ---- register ----
    reg = sub.add_parser("register", help="启动注册任务")
    reg.add_argument("-n", "--count", type=int, default=1, help="注册账号总数 (默认: 1)")
    reg.add_argument("-j", "--jobs", type=int, default=1, help="并发数 (默认: 1)")
    reg.add_argument("-c", "--config", type=str, default=None, help="配置文件路径")
    reg.add_argument("--local-proxy", type=str, default=None, help="本地代理地址")

    # ---- web ----
    web = sub.add_parser("web", help="启动 Web 控制面板")
    web.add_argument("--host", type=str, default="0.0.0.0", help="监听地址 (默认: 0.0.0.0)")
    web.add_argument("--port", type=int, default=8080, help="监听端口 (默认: 8080)")
    web.add_argument("-c", "--config", type=str, default=None, help="配置文件路径")

    # ---- 无子命令时显示帮助 ----
    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    # 配置文件路径覆盖
    if hasattr(args, "config") and args.config:
        config_path = Path(args.config)
        if not config_path.is_absolute():
            config_path = Path.cwd() / config_path
        os.environ["TREZA_CONFIG_PATH"] = str(config_path.resolve())

    if args.command == "register":
        _cmd_register(args)
    elif args.command == "web":
        _cmd_web(args)


def _cmd_register(args) -> None:
    email_backend = CloudflareEmail(verify=False)
    proxy_pool = ProxyPool(jobs_hint=args.jobs)

    if args.local_proxy:
        proxy_pool._local = _normalize_proxy(args.local_proxy)
        proxy_pool._enabled = True

    try:
        asyncio.run(run_register(args.count, args.jobs, email_backend, proxy_pool))
    except KeyboardInterrupt:
        print("\n已退出.")
        sys.exit(0)


def _cmd_web(args) -> None:
    import uvicorn
    from .web_server import app

    print(f"\n  TrezaReg Panel 启动: http://{args.host}:{args.port}")
    print(f"  账号库: {_ROOT / 'accounts'}\n")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


def _normalize_proxy(proxy: str) -> str:
    proxy = proxy.strip()
    if proxy and "://" not in proxy:
        proxy = "http://" + proxy
    return proxy


if __name__ == "__main__":
    main()
