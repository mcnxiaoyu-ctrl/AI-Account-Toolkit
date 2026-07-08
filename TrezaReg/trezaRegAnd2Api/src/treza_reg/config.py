"""配置文件加载 — 从 config.yaml 读取全部配置，环境变量可覆盖邮箱凭据."""

import os
from pathlib import Path
from typing import Any

import yaml
from ruamel.yaml import YAML as RuamelYAML

_ROOT = Path(__file__).resolve().parent.parent.parent


def _load_yaml() -> dict[str, Any]:
    config_path = _ROOT / "config.yaml"
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


_YAML = _load_yaml()


def _yml(section: str, key: str, default: Any = "") -> Any:
    return _YAML.get(section, {}).get(key, default)


# ---- Cloudflare 临时邮箱 ----
CF_EMAIL_BASE_URL = _env("CF_EMAIL_BASE_URL") or _yml("cf_email", "base_url", "https://email.banyin.asia")
CF_EMAIL_DOMAIN = _env("CF_EMAIL_DOMAIN") or _yml("cf_email", "domain", "banyin.asia")
CF_EMAIL_LOGIN_EMAIL = _env("CF_EMAIL_LOGIN_EMAIL") or _yml("cf_email", "login_email", "")
CF_EMAIL_LOGIN_PASSWORD = _env("CF_EMAIL_LOGIN_PASSWORD") or _yml("cf_email", "login_password", "")
CF_EMAIL_ADMIN_AUTH = _env("CF_EMAIL_ADMIN_AUTH") or _yml("cf_email", "admin_auth", "")

# ---- 代理 ----
PROXY_ENABLED: bool = _yml("proxy", "enabled", False)
PROXY_API_URL: str = _yml("proxy", "api_url", "")
PROXY_TEST_URLS: list[str] = _yml("proxy", "test_urls", ["https://www.google.com/"])
PROXY_TEST_TIMEOUT: int = int(_yml("proxy", "test_timeout", 8))

# ---- Treza API ----
PRIVY_APP_ID: str = _yml("treza", "privy_app_id", "cmc3qreh800pfjs0lxodhz0of")
PRIVY_CA_ID: str = _yml("treza", "privy_ca_id", "b06bd3bf-1f77-442e-9e45-66b57266dc23")
INIT_URL: str = _yml("treza", "init_url", "https://auth.privy.io/api/v1/passwordless/init")
AUTH_URL: str = _yml("treza", "auth_url", "https://auth.privy.io/api/v1/passwordless/authenticate")
TREZA_ORIGIN: str = _yml("treza", "origin", "https://www.trezalabs.com")

# ---- 注册参数 ----
POLL_TIMEOUT: int = int(_yml("registration", "poll_timeout", 120))
POLL_INTERVAL: int = int(_yml("registration", "poll_interval", 5))
REQUEST_TIMEOUT: int = int(_yml("registration", "request_timeout", 30))

# ---- 注册中心 ----
_rc = _YAML.get("register_center", {})

REG_CENTER_AUTO_START_BALANCED: bool = _rc.get("auto_start_balanced", False)
REG_CENTER_AUTO_START_SCHEDULED: bool = _rc.get("auto_start_scheduled", False)


def get_register_center_section(section: str) -> dict[str, Any]:
    """读取 register_center 下的子配置节 (balanced / scheduled)."""
    return dict(_rc.get(section, {}))


def save_register_center_config(section: str, data: Any) -> None:
    """保存 register_center 配置到 config.yaml (保留注释和格式)."""
    config_path = _ROOT / "config.yaml"
    ryaml = RuamelYAML()
    ryaml.preserve_quotes = True

    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            yaml_data = ryaml.load(f)
    else:
        yaml_data = {}

    if yaml_data is None:
        yaml_data = {}
    if "register_center" not in yaml_data:
        yaml_data["register_center"] = {}

    rc = yaml_data["register_center"]
    if isinstance(data, dict) and section in ("balanced", "scheduled"):
        if section not in rc:
            rc[section] = {}
        rc[section].update(data)
    else:
        rc[section] = data

    with open(config_path, "w", encoding="utf-8") as f:
        ryaml.dump(yaml_data, f)

    # 刷新内存缓存 (下次读取时生效)
    global _rc, REG_CENTER_AUTO_START_BALANCED, REG_CENTER_AUTO_START_SCHEDULED
    if section == "auto_start_balanced":
        REG_CENTER_AUTO_START_BALANCED = bool(data)
    elif section == "auto_start_scheduled":
        REG_CENTER_AUTO_START_SCHEDULED = bool(data)
    _rc = rc


# ---- 输出 ----
ACCOUNTS_DIR: str = _yml("output", "accounts_dir", "accounts")


def get_accounts_dir() -> Path:
    """返回账号库绝对路径."""
    p = Path(ACCOUNTS_DIR)
    if not p.is_absolute():
        p = _ROOT / ACCOUNTS_DIR
    p.mkdir(parents=True, exist_ok=True)
    return p
