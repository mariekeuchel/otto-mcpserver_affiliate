"""Konfiguration über Umgebungsvariablen (Cloud Run / Secret Manager kompatibel)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_list(name: str) -> list[str]:
    raw = _env(name)
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


@dataclass(frozen=True)
class Settings:
    # --- OTTO Partnerprogramm (easy.marketing Publisher-API) ---
    base_url: str = "https://partnerprogramm.otto.de"
    access_token: str | None = None
    publisher_id: str | None = None
    api_format: str = "csv"  # csv | json
    timeout_seconds: float = 60.0
    cache_ttl_seconds: int = 300

    # Optionale Überschreibungen der Spaltennamen, falls die automatische
    # Erkennung bei den CSV/JSON-Spalten nicht greift.
    field_overrides: dict[str, list[str]] = field(default_factory=dict)

    # --- MCP-Server ---
    mcp_auth_tokens: list[str] = field(default_factory=list)
    host: str = "0.0.0.0"
    port: int = 8080
    mcp_path: str = "/mcp"

    @property
    def is_configured(self) -> bool:
        return bool(self.access_token and self.publisher_id)

    @classmethod
    def from_env(cls) -> Settings:
        overrides: dict[str, list[str]] = {}
        for key in (
            "turnover", "attributed_turnover", "commission", "status", "date", "order_id",
            "admedia", "event", "payout_date", "referrer", "subid",
        ):
            values = _env_list(f"OTTO_FIELD_{key.upper()}")
            if values:
                overrides[key] = values

        api_format = (_env("OTTO_API_FORMAT", "csv") or "csv").lower()
        if api_format not in ("csv", "json"):
            raise ValueError("OTTO_API_FORMAT muss 'csv' oder 'json' sein")

        return cls(
            base_url=(_env("OTTO_API_BASE_URL", cls.base_url) or cls.base_url).rstrip("/"),
            access_token=_env("OTTO_API_ACCESS_TOKEN"),
            publisher_id=_env("OTTO_PUBLISHER_ID"),
            api_format=api_format,
            timeout_seconds=float(_env("OTTO_API_TIMEOUT", "60") or 60),
            cache_ttl_seconds=int(_env("OTTO_CACHE_TTL", "300") or 300),
            field_overrides=overrides,
            mcp_auth_tokens=_env_list("MCP_AUTH_TOKEN"),
            host=_env("HOST", "0.0.0.0") or "0.0.0.0",
            port=int(_env("PORT", "8080") or 8080),
            mcp_path=_env("MCP_PATH", "/mcp") or "/mcp",
        )
