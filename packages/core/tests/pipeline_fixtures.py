"""Small shared configuration for offline pipeline regression tests."""

from wenyi_core.config import Config
from wenyi_core.ingest.models import Segment
from wenyi_core.pipeline.translation_batch import BatchPlan


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


def batch_plan(sources, *, allow_empty=False):
    return BatchPlan.capture(
        3,
        5,
        [Segment(index=10 + i, source=s) for i, s in enumerate(sources)],
        [],
        "previous translation",
        "style",
        "synopsis",
        "digest",
        [[] for _ in sources],
        "next source",
        allow_empty_translations=allow_empty,
    )
