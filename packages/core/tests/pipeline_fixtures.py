"""Shared configuration and storage narrowing for offline pipeline tests."""

from wenyi_core.config import Config
from wenyi_core.storage.file import FileStorage
from wenyi_core.storage.protocol import Storage


def require_file_storage(store: Storage) -> FileStorage:
    """CLI/offline tests use the file backend; narrow Storage to FileStorage for path asserts."""
    if not isinstance(store, FileStorage):
        raise TypeError(f"expected FileStorage, got {type(store).__name__}")
    return store


def fake_agent_config() -> Config:
    return Config.from_dict(
        {
            "language": {"source": "ja", "target": "zh"},
            "llm": {
                "preset": "fake",
                "models": {
                    "default_strong": {"provider": "default", "model": "p"},
                    "default_cheap": {"provider": "default", "model": "f"},
                },
            },
        }
    )


def fake_pipeline_config(state_dir: str) -> Config:
    config = fake_agent_config()
    config.segment.max_tokens_per_batch = 1800
    config.pipeline.review = True
    config.pipeline.review_autofix = False
    config.pipeline.polish = True
    config.state_dir = state_dir
    return config
