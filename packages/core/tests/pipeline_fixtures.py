"""Small shared configuration for offline pipeline regression tests."""

from wenyi_core.config import Config


def fake_pipeline_config(state_dir: str) -> Config:
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
            "segment": {"max_tokens_per_batch": 1800},
            "pipeline": {
                "review": True,
                "review_autofix": False,
                "polish": True,
            },
            "paths": {"state_dir": state_dir},
        }
    )
