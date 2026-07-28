"""API 运行配置（全部走环境变量）。"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    database_url: str = _env(
        "DATABASE_URL", "postgresql://wenyi:wenyi@localhost:5432/wenyi"
    )
    redis_url: str = _env("REDIS_URL", "redis://localhost:6379/0")
    data_dir: str = _env("DATA_DIR", "./data")
    # 可选静态 Token（v1 无用户系统，防公网裸奔）
    api_token: str | None = _env("WENYI_API_TOKEN", "") or None
    # 内核 LLM 配置走环境变量（DEEPSEEK_API_KEY 等），透传给 wenyi-core。
    # Worker 构造 Config 时从 config.yaml 读取默认，再用项目策略覆盖。
    config_path: str = _env("WENYI_CONFIG", "config.yaml")

    @property
    def psycopg_dsn(self) -> str:
        """asyncpg/SQLAlchemy 用 postgresql://...；psycopg3 同样接受。"""
        url = self.database_url
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        return url


settings = Settings()
