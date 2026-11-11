"""Settings, read from environment variables (prefix NOUS_DRIVE_)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(f"NOUS_DRIVE_{name}", default)


def _int(name: str, default: int) -> int:
    v = _env(name)
    return int(v) if v else default


def _bool(name: str, default: bool) -> bool:
    v = _env(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", "postgresql://localhost/nous_drive"))
    # Fernet key (urlsafe base64, 32 bytes) for OAuth tokens at rest.
    encryption_key: str | None = field(default_factory=lambda: _env("ENCRYPTION_KEY"))
    # Used to sign OAuth state parameters.
    secret_key: str = field(default_factory=lambda: _env("SECRET_KEY", "dev-insecure-secret-change-me"))
    public_base_url: str = field(default_factory=lambda: _env("PUBLIC_BASE_URL", "http://localhost:8080"))

    # Google OAuth / Drive
    google_client_id: str | None = field(default_factory=lambda: _env("GOOGLE_CLIENT_ID"))
    google_client_secret: str | None = field(default_factory=lambda: _env("GOOGLE_CLIENT_SECRET"))
    drive_api_base: str = field(default_factory=lambda: _env("DRIVE_API_BASE", "https://www.googleapis.com/drive/v3"))
    google_token_url: str = field(default_factory=lambda: _env("GOOGLE_TOKEN_URL", "https://oauth2.googleapis.com/token"))
    google_auth_url: str = field(
        default_factory=lambda: _env("GOOGLE_AUTH_URL", "https://accounts.google.com/o/oauth2/v2/auth")
    )
    drive_page_size: int = field(default_factory=lambda: _int("DRIVE_PAGE_SIZE", 1000))
    drive_poll_seconds: int = field(default_factory=lambda: _int("DRIVE_POLL_SECONDS", 300))
    drive_channel_ttl_seconds: int = field(default_factory=lambda: _int("DRIVE_CHANNEL_TTL_SECONDS", 86400 * 6))
    drive_channel_renew_before_seconds: int = field(default_factory=lambda: _int("DRIVE_CHANNEL_RENEW_BEFORE", 3600 * 6))
    drive_max_requests_per_second: float = field(
        default_factory=lambda: float(_env("DRIVE_MAX_RPS", "8"))
    )
    push_enabled: bool = field(default_factory=lambda: _bool("PUSH_ENABLED", True))
    reconcile_hours: int = field(default_factory=lambda: _int("RECONCILE_HOURS", 24))

    # Retention / health
    retention_days: int = field(default_factory=lambda: _int("RETENTION_DAYS", 90))
    device_offline_seconds: int = field(default_factory=lambda: _int("DEVICE_OFFLINE_SECONDS", 180))
    degraded_alert_minutes: int = field(default_factory=lambda: _int("DEGRADED_ALERT_MINUTES", 30))
    device_offline_alert_hours: int = field(default_factory=lambda: _int("DEVICE_OFFLINE_ALERT_HOURS", 24))

    # Notifications
    notify_window_seconds: int = field(default_factory=lambda: _int("NOTIFY_WINDOW_SECONDS", 180))
    slack_webhook_url: str | None = field(default_factory=lambda: _env("SLACK_WEBHOOK_URL"))
    notify_webhook_url: str | None = field(default_factory=lambda: _env("NOTIFY_WEBHOOK_URL"))
    smtp_host: str | None = field(default_factory=lambda: _env("SMTP_HOST"))
    smtp_port: int = field(default_factory=lambda: _int("SMTP_PORT", 587))
    smtp_user: str | None = field(default_factory=lambda: _env("SMTP_USER"))
    smtp_password: str | None = field(default_factory=lambda: _env("SMTP_PASSWORD"))
    smtp_from: str | None = field(default_factory=lambda: _env("SMTP_FROM"))
    notify_email_to: str | None = field(default_factory=lambda: _env("NOTIFY_EMAIL_TO"))

    # Phase 2 (content)
    content_enabled: bool = field(default_factory=lambda: _bool("CONTENT_ENABLED", False))
    content_max_bytes: int = field(default_factory=lambda: _int("CONTENT_MAX_BYTES", 50 * 1024 * 1024))
    content_max_chars: int = field(default_factory=lambda: _int("CONTENT_MAX_CHARS", 2_000_000))
    embedding_provider: str = field(default_factory=lambda: _env("EMBEDDING_PROVIDER", "local"))
    voyage_api_key: str | None = field(default_factory=lambda: os.environ.get("VOYAGE_API_KEY"))
    voyage_model: str = field(default_factory=lambda: _env("VOYAGE_MODEL", "voyage-3"))
    summary_provider: str = field(default_factory=lambda: _env("SUMMARY_PROVIDER", "claude"))
    summary_model: str = field(default_factory=lambda: _env("SUMMARY_MODEL", "claude-opus-5-5"))

    # Worker
    worker_concurrency: int = field(default_factory=lambda: _int("WORKER_CONCURRENCY", 4))
    worker_poll_seconds: float = field(default_factory=lambda: float(_env("WORKER_POLL_SECONDS", "1.0")))

    # MCP
    mcp_page_limit: int = field(default_factory=lambda: _int("MCP_PAGE_LIMIT", 200))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings() -> None:
    get_settings.cache_clear()
