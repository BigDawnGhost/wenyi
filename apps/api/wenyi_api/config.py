"""API runtime settings loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    database_url: str = _env("DATABASE_URL", "postgresql://wenyi:wenyi@localhost:5432/wenyi")
    redis_url: str = _env("REDIS_URL", "redis://localhost:6379/0")
    data_dir: str = _env("DATA_DIR", "./data")
    # Optional static token for deployments without individual user accounts.
    api_token: str | None = _env("WENYI_API_TOKEN", "") or None
    # Core provider credentials come from environment variables such as DEEPSEEK_API_KEY.
    # Workers load defaults from config.yaml, then apply project overrides.
    config_path: str = _env("WENYI_CONFIG", "config.yaml")

    @property
    def psycopg_dsn(self) -> str:
        """Normalize the PostgreSQL connection URL for psycopg."""
        url = self.database_url
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://") :]
        return url


settings = Settings()
