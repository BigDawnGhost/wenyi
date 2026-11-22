"""Chunked PDF fillback: boundaries, progress events, failure isolation and resume."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from wenyi_core.assemble.pdf_writer import _assemble_pdf
from wenyi_core.ingest.pdf_babeldoc import FILLBACK_CHUNK_SIZE, fillback_chunked
from wenyi_core.pdf_bridge import BabeldocBridgeError


class FakeBridge:
    """Bridge stand-in that accumulates translations per session, like the real service."""

    def __init__(self, fail_on_call: int | None = None):
        self.calls: list[dict[str, str]] = []
        self.targets: list[Path] = []
        self.fail_on_call = fail_on_call
        self.applied: dict[str, str] = {}

    def fillback(self, session_id, translations, *, out_path, **options):
        self.calls.append(dict(translations))
        self.targets.append(Path(out_path))
        if self.fail_on_call is not None and len(self.calls) >= self.fail_on_call:
            raise BabeldocBridgeError("fillback request failed: synthetic")
        self.applied.update(translations)
        Path(out_path).write_bytes(b"%PDF-1.4 fake")
        return str(out_path)


def test_chunks_follow_document_order_and_cover_every_paragraph(tmp_path):
    bridge = FakeBridge()
    translations = {f"{index}:0": f"t{index}" for index in range(5)}
    result = fillback_chunked(
        bridge, "session", translations, out_path=str(tmp_path / "out.pdf"), chunk_size=2
    )
    assert [list(call) for call in bridge.calls] == [["0:0", "1:0"], ["2:0", "3:0"], ["4:0"]]
    assert bridge.applied == translations
    assert result == str(tmp_path / "out.pdf")


def test_progress_reports_chunk_granularity(tmp_path):
    events = []
    bridge = FakeBridge()
    translations = {f"{index}:0": "t" for index in range(4)}
    fillback_chunked(
        bridge,
        "session",
        translations,
        out_path=str(tmp_path / "out.pdf"),
        chunk_size=2,
        progress=lambda done, total, label: events.append((done, total, label)),
    )
    assert events == [
        (0, 2, "progress.stage.assembling"),
        (1, 2, "progress.stage.assembling"),
        (2, 2, "progress.stage.assembling"),
    ]


def test_intermediate_chunks_stay_private_and_leave_no_scratch_files(tmp_path):
    bridge = FakeBridge()
    translations = {f"{index}:0": "t" for index in range(5)}
    out = tmp_path / "out.pdf"
    fillback_chunked(bridge, "session", translations, out_path=str(out), chunk_size=2)
    assert all(target != out for target in bridge.targets[:-1])
    assert all(target.parent == tmp_path for target in bridge.targets)
    assert bridge.targets[-1] == out
    assert list(tmp_path.iterdir()) == [out]
    assert out.read_bytes() == b"%PDF-1.4 fake"


def test_mid_chunk_failure_keeps_target_untouched_and_cleans_scratch(tmp_path):
    bridge = FakeBridge(fail_on_call=2)
    translations = {f"{index}:0": "t" for index in range(5)}
    out = tmp_path / "out.pdf"
    progress_calls = []
    with pytest.raises(BabeldocBridgeError):
        fillback_chunked(
            bridge,
            "session",
            translations,
            out_path=str(out),
            chunk_size=2,
            progress=lambda done, total, label: progress_calls.append(done),
        )
    assert not out.exists()
    assert list(tmp_path.iterdir()) == []
    assert len(bridge.calls) == 2
    assert progress_calls == [0, 1]


def test_resume_after_failure_replays_every_chunk(tmp_path):
    translations = {f"{index}:0": f"t{index}" for index in range(4)}
    out = tmp_path / "out.pdf"
    failing = FakeBridge(fail_on_call=2)
    with pytest.raises(BabeldocBridgeError):
        fillback_chunked(failing, "session", translations, out_path=str(out), chunk_size=2)
    bridge = FakeBridge()
    fillback_chunked(bridge, "session", translations, out_path=str(out), chunk_size=2)
    assert bridge.calls == [{"0:0": "t0", "1:0": "t1"}, {"2:0": "t2", "3:0": "t3"}]
    assert bridge.applied == translations
    assert out.exists()


def test_single_chunk_writes_the_target_directly(tmp_path):
    bridge = FakeBridge()
    events = []
    fillback_chunked(
        bridge,
        "session",
        {"0:0": "译文"},
        out_path=str(tmp_path / "out.pdf"),
        progress=lambda done, total, label: events.append((done, total)),
    )
    assert bridge.calls == [{"0:0": "译文"}]
    assert bridge.targets == [tmp_path / "out.pdf"]
    assert events == [(0, 1), (1, 1)]


def test_default_chunk_size_splits_large_documents(tmp_path):
    bridge = FakeBridge()
    translations = {f"{index}:0": "t" for index in range(FILLBACK_CHUNK_SIZE + 1)}
    fillback_chunked(bridge, "session", translations, out_path=str(tmp_path / "out.pdf"))
    assert len(bridge.calls) == 2
    assert len(bridge.calls[0]) == FILLBACK_CHUNK_SIZE
    assert len(bridge.calls[1]) == 1


def test_invalid_chunk_size_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        fillback_chunked(
            FakeBridge(),
            "session",
            {"0:0": "t"},
            out_path=str(tmp_path / "out.pdf"),
            chunk_size=0,
        )


def test_assemble_pdf_routes_fillback_through_the_chunked_driver(tmp_path):
    bridge = FakeBridge()
    translations = {f"{index}:0": "t" for index in range(FILLBACK_CHUNK_SIZE + 1)}
    progress = MagicMock()
    state = ("session", "http://bridge.test", translations, {})
    with (
        patch("wenyi_core.assemble.pdf_writer.fillback_from_store", return_value=state),
        patch("wenyi_core.assemble.pdf_writer.BabeldocBridgeClient", return_value=bridge),
    ):
        result = _assemble_pdf(
            MagicMock(), "source.pdf", str(tmp_path / "out.pdf"), timeout=5, progress=progress
        )
    assert result == str(tmp_path / "out.pdf")
    assert len(bridge.calls) == 2
    progress.assert_any_call(0, 2, "progress.stage.assembling")
    progress.assert_any_call(2, 2, "progress.stage.assembling")
