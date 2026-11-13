"""Application configuration.

Every value comes from the environment / .env file. Nothing operational is hardcoded.
RPC endpoints are resolved dynamically from ``RPC_<CHAIN_KEY>`` so new chains need no code change.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _csv(v: object) -> list[str]:
    if v is None or v == "":
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [p.strip() for p in str(v).split(",") if p.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ─────────────────────────── core ───────────────────────────
    app_env: str = "production"
    tz: str = "UTC"
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"
    secret_key: str = "change-me-in-dotenv"

    # ───────────────────────── database ─────────────────────────
    database_url: str = "postgresql+psycopg://faucet:faucet@db:5432/faucet"
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_echo: bool = False
    db_statement_timeout_ms: int = 30_000

    # ──────────────────────── dashboard ─────────────────────────
    dashboard_enabled: bool = True
    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 8000
    dashboard_username: str = "admin"
    # Prefer a bcrypt hash. A plaintext DASHBOARD_PASSWORD is hashed at startup and never persisted.
    dashboard_password_hash: str = ""
    dashboard_password: str = ""

    # ──────────────────────── scheduler ─────────────────────────
    run_scheduler: bool = False
    faucet_probe_interval_min: int = 20
    discover_interval_hours: int = 24
    eligibility_interval_hours: int = 6
    claim_readiness_interval_min: int = 30
    detect_incoming_interval_min: int = 30
    cost_refresh_interval_min: int = 60

    # ── incoming-value detection ──
    # Chains with an Alchemy endpoint use the indexed API (one request, full history).
    # Everything else falls back to chunked eth_getLogs, bounded by these two settings
    # so a scan stays cheap: chunk x chunks blocks of history are examined per run.
    scan_chunk_blocks: int = 10_000
    scan_max_chunks: int = 12
    scan_max_transfers: int = 100
    # Select chains compatible with your RPC provider and request budget.
    detect_chains: str = "base,arbitrum,polygon,zksync,ethereum,plasma"
    daily_summary_hour: int = 9
    retention_interval_hours: int = 24
    job_timeout_seconds: int = 600
    scheduler_misfire_grace_seconds: int = 300

    # ──────────────── http fetch policy (must be obeyed) ────────────────
    http_timeout_seconds: float = 15.0
    http_connect_timeout_seconds: float = 8.0
    http_max_retries: int = 3
    http_backoff_base: float = 1.5
    http_backoff_max_seconds: float = 60.0
    http_user_agent: str = "FaucetMonitor/1.0 (+contact: unset)"
    http_max_concurrent: int = 4
    rate_limit_per_host_per_min: int = 6
    respect_robots_txt: bool = True
    verify_tls: bool = True

    # Optional fallback proxy after a direct transport failure.
    http_proxy_url: str = ""
    http_proxy_fallback: bool = True
    # Optional host suffixes to route through the configured proxy first.
    http_proxy_only_hosts: str = ""

    # ─────────────────────────── chains ───────────────────────────
    # Individual endpoints live in RPC_<CHAIN_KEY> env vars, e.g. RPC_ETH_SEPOLIA.
    rpc_timeout_seconds: float = 10.0
    rpc_max_retries: int = 2
    chains_config_path: str = "config/chains.yaml"

    # ────────────────────────── pricing ──────────────────────────
    price_provider: Literal["static", "coingecko"] = "static"
    static_prices_json: str = "{}"
    coingecko_api_key: str = ""
    coingecko_base_url: str = "https://api.coingecko.com/api/v3"

    # ──────────────────────── notifications ────────────────────────
    notify_enabled: bool = False
    notify_channels: str = ""  # csv: feishu,dingtalk,wecom,serverchan,bark,smtp,telegram
    notify_min_interval_seconds: int = 300
    notify_max_per_hour: int = 12

    feishu_webhook_url: str = ""
    feishu_secret: str = ""
    dingtalk_webhook_url: str = ""
    dingtalk_secret: str = ""
    wecom_webhook_url: str = ""
    serverchan_sendkey: str = ""
    bark_url: str = ""

    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_to: str = ""
    smtp_use_ssl: bool = True

    # Telegram supports a separately configured optional proxy.
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_allowed_user_ids: str = ""
    telegram_proxy: str = ""
    telegram_poll_enabled: bool = False
    telegram_api_base: str = "https://api.telegram.org"

    # ─────────────── auto-claim (default: fully OFF) ───────────────
    global_auto_claim: bool = False
    auto_claim_dry_run: bool = True
    auto_claim_require_allowlist: bool = True
    auto_claim_max_retries: int = 2
    auto_claim_min_interval_seconds: int = 300
    auto_claim_max_usd: float = 0.0

    # ── 领取成本参考阀值 ──
    # 只用于「现在值不值得领」的建议，用来调整通知语气。
    # 绝不控制任何执行 —— 执行由 9 道门槛决定，与此无关。
    # 测试网链无视这两个值（gas 是测试币，无实际成本）。
    claim_gas_max_gwei: float = 30.0
    claim_cost_max_usd: float = 5.0

    # ───────────────────────── retention ─────────────────────────
    retain_faucet_checks_days: int = 90
    retain_notifications_days: int = 180
    retain_scan_logs_days: int = 30
    retain_eligibility_per_pair: int = 10

    # ──────────────────────── source plugins ────────────────────────
    sources_config_path: str = "config/sources.yaml"
    wallets_config_path: str = "config/wallets.yaml"
    github_token: str = ""  # optional; raises API rate limit
    discovery_enabled_sources: str = "manual,rss"

    # ─────────────────────────── validators ───────────────────────────
    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    # ─────────────────────────── helpers ───────────────────────────
    @property
    def notify_channel_list(self) -> list[str]:
        return _csv(self.notify_channels)

    @property
    def source_plugin_list(self) -> list[str]:
        return _csv(self.discovery_enabled_sources)

    @property
    def detect_chain_list(self) -> list[str]:
        return _csv(self.detect_chains)

    @property
    def proxy_only_host_list(self) -> list[str]:
        return [h.lower().lstrip(".") for h in _csv(self.http_proxy_only_hosts)]

    def host_prefers_proxy(self, host: str) -> bool:
        host = (host or "").lower().split(":")[0]
        if not host:
            return False
        return any(host == h or host.endswith("." + h) for h in self.proxy_only_host_list)

    @property
    def telegram_allowed_id_list(self) -> list[int]:
        out: list[int] = []
        for x in _csv(self.telegram_allowed_user_ids):
            try:
                out.append(int(x))
            except ValueError:
                pass
        return out

    @property
    def static_prices(self) -> dict[str, float]:
        import json

        try:
            raw = json.loads(self.static_prices_json or "{}")
        except json.JSONDecodeError:
            return {}
        return {str(k).upper(): float(v) for k, v in raw.items() if _is_number(v)}

    def rpc_url(self, chain_key: str) -> str | None:
        """Resolve ``RPC_<CHAIN_KEY>`` for a chain slug like ``eth-sepolia`` -> ``RPC_ETH_SEPOLIA``."""
        env_name = "RPC_" + chain_key.upper().replace("-", "_").replace(".", "_")
        value = os.environ.get(env_name, "").strip()
        return value or None

    def rpc_env_name(self, chain_key: str) -> str:
        return "RPC_" + chain_key.upper().replace("-", "_").replace(".", "_")


def _is_number(v: object) -> bool:
    try:
        float(v)  # type: ignore[arg-type]
        return True
    except (TypeError, ValueError):
        return False


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

