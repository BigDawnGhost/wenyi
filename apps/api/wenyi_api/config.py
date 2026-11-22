"""API runtime settings loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


# Explicit values that switch a boolean flag off. Any other value, including a
# typo such as "fals", must fail closed and keep the protection enabled.
_FLAG_OFF_VALUES = {"0", "false", "no", "off"}


def _env_bool(name: str, default: bool) -> bool:
    """Parse a boolean environment flag; unrecognized values keep it enabled."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in _FLAG_OFF_VALUES


@dataclass(frozen=True)
class Settings:
    database_url: str = _env("DATABASE_URL", "postgresql://wenyi:wenyi@localhost:5432/wenyi")
    # Depth of the shared pool used by short request and state connections.
    # Long workflow/export locks use dedicated connections, so this must cover
    # request threads plus every concurrent job's brief state writes; raise it
    # together with worker max_jobs (workflow + export concurrency).
    db_pool_max_size: int = int(_env("WENYI_DB_POOL_MAX_SIZE", "16"))
    redis_url: str = _env("REDIS_URL", "redis://localhost:6379/0")
    data_dir: str = _env("DATA_DIR", "./data")
    # Optional static token for deployments without individual user accounts.
    api_token: str | None = _env("WENYI_API_TOKEN", "") or None
    # Require bearer-token authentication whenever api_token is configured.
    # Defaults to enabled; only the explicit off values above disable it, so an
    # empty or unrecognized WENYI_REQUIRE_TOKEN never silently turns auth off.
    require_token: bool = _env_bool("WENYI_REQUIRE_TOKEN", True)
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
