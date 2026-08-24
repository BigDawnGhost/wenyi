"""字幕翻译：平行于书本 pipeline 的轻量路径。

``ingest.srt_reader`` / ``assemble.srt_writer`` 负责读写；本包负责
``state/srt/<slug>/`` 状态与滑窗并发翻译。不依赖 Orchestrator、术语库或 Review。
"""

from .store import SrtRunStore
from .translate import translate_srt

__all__ = [
    "SrtRunStore",
    "translate_srt",
]
