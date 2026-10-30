"""Published precision draft queries use only an isolated artifact store."""

from contextlib import contextmanager
from copy import deepcopy
from unittest.mock import Mock

import pytest
from wenyi_core.ingest.models import Chapter, Segment
from wenyi_core.storage.precision_archive import PrecisionArchive
from wenyi_core.storage.precision_drafts import _identity, read_precision_drafts
from wenyi_core.storage.protocol import Storage


class Store:
    def __init__(self):
        self.chapter = Chapter(
            index=4,
            segments=[
                Segment(index=8, source="first", target="final", target_before_polish="one"),
                Segment(index=31, source="second", target="end", target_before_polish="uno"),
            ],
        )
        self.artifacts = {}
        self.reads = []
        self.prefixes = []
        self.port = Mock(spec=Storage, wraps=self)

    @contextmanager
    def state_lock(self):
        yield

    def load_chapter(self, ci):
        assert ci == 4
        return self.chapter

    def load_manifest(self):
        return {"source_sha256": "a" * 64}

    def read_artifact(self, key):
        self.reads.append(key)
        return deepcopy(self.artifacts.get(key))

    def write_artifact(self, key, value):
        self.artifacts[key] = deepcopy(value)

    def list_artifacts(self, prefix):
        self.prefixes.append(prefix)
        return [key for key in self.artifacts if key.startswith(prefix)]


def publish(store, *, legacy=False, final=None, t2=None):
    final = final or ["final", "end"]
    before = ["one", "uno"]
    binding = {"source_sha256": "a" * 64, "plan": {"sources": ["first", "second"]}}
    fingerprint = _identity(binding) if legacy else _identity([final, t2])
    root = f"precision/chapters/4/0-2/{fingerprint}"
    archive = PrecisionArchive(store)

    def record(stage, payload):
        store.write_artifact(
            f"{root}/{stage}.json",
            {
                "stage": stage,
                "fingerprint": fingerprint,
                "payload_hash": _identity(payload),
                "payload": payload,
            },
        )

    stage = "ready" if legacy else "result"
    record(
        stage,
        {"targets": final, "draft": before}
        if legacy
        else {"targets_ref": archive.put(final), "draft_ref": archive.put(before)},
    )
    for name, targets in zip(("T1", "T2", "T3"), (before, t2 or ["two", "dos"], ["three", "tres"])):
        record(
            f"drafts/{name}",
            {"targets": targets} if legacy else {"targets_ref": archive.put(targets)},
        )
    publication = {
        "status": "published",
        "fingerprint": fingerprint,
        "source_sha256": "a" * 64,
        "segment_indices": [8, 31],
        "target_hash": _identity(final),
        "result_ref": f"{root}/{stage}.json",
    }
    if legacy:
        store.write_artifact(f"{root}/inputs.json", binding)
    else:
        publication["source_hashes"] = [_identity("first"), _identity("second")]
    store.write_artifact(f"{root}/publication.json", publication)
    return root


@pytest.mark.parametrize("legacy", [False, True])
def test_stable_identity_and_human_edit_read_only(legacy):
    store = Store()
    publish(store, legacy=legacy)
    store.chapter.segments[1].target = "human edit"
    snapshot = deepcopy(store.artifacts)
    result = read_precision_drafts(store.port, 4, 31)
    assert result.available
    assert [item.target for item in result.candidates] == ["uno", "dos", "tres"]
    assert result.synthesized_target == "end"
    assert result.before_polish_candidate == "T1"
    assert store.artifacts == snapshot
    assert store.prefixes == ["precision/chapters/4/"]
    store.port.write_artifact.assert_not_called()
    with pytest.raises(KeyError):
        read_precision_drafts(store.port, 4, 1)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("source", "source_mismatch"),
        ("book", "source_mismatch"),
        ("t2", "incomplete"),
        ("hash", "corrupt"),
        ("reference", "corrupt"),
        ("before", "incomplete"),
        ("unpublished", "incomplete"),
    ],
)
def test_unavailable_never_leaks_candidates(change, reason):
    store = Store()
    root = publish(store)
    publication = store.artifacts[f"{root}/publication.json"]
    if change == "source":
        store.chapter.segments[0].source = "changed"
    elif change == "book":
        publication["source_sha256"] = "b" * 64
    elif change == "t2":
        del store.artifacts[f"{root}/drafts/T2.json"]
    elif change == "hash":
        store.artifacts[f"{root}/result.json"]["payload_hash"] = "bad"
    elif change == "reference":
        ref = store.artifacts[f"{root}/drafts/T1.json"]["payload"]["targets_ref"]
        store.artifacts[ref]["value"][0] = "tampered"
    elif change == "before":
        store.chapter.segments[0].target_before_polish = None
    else:
        publication["status"] = "ready"
        store.chapter.segments[0].target = None
    result = read_precision_drafts(store.port, 4, 8)
    assert not result.available
    assert result.reason == reason
    assert result.candidates == []
    assert result.synthesized_target is None


def test_ambiguity_and_exact_current_target_preference():
    store = Store()
    publish(store)
    publish(store, final=["other", "other end"], t2=["new two", "new dos"])
    assert read_precision_drafts(store.port, 4, 8).synthesized_target == "final"
    store.chapter.segments[0].target = "human"
    assert read_precision_drafts(store.port, 4, 8).reason == "ambiguous"


def test_prefix_and_batch_filter_bound_reads():
    store = Store()
    root = publish(store)
    for ci, batch in [(5, "0-2"), (4, "100-2")]:
        store.artifacts[f"precision/chapters/{ci}/{batch}/{'c' * 64}/publication.json"] = {}
    store.reads.clear()
    assert read_precision_drafts(store.port, 4, 8).available
    assert all(key.startswith(root) or key.startswith("precision/shared/") for key in store.reads)


def test_ready_only_when_formal_commit_completed():
    store = Store()
    root = publish(store)
    store.artifacts[f"{root}/publication.json"]["status"] = "ready"
    assert read_precision_drafts(store.port, 4, 8).available
    store.chapter.segments[0].target = "not committed result"
    assert read_precision_drafts(store.port, 4, 8).reason == "incomplete"


def test_same_synthesis_different_candidates_is_ambiguous():
    store = Store()
    publish(store)
    publish(store, t2=["different", "draft"])
    assert read_precision_drafts(store.port, 4, 8).reason == "ambiguous"


def test_missing_archive_and_nontext_segment():
    store = Store()
    assert read_precision_drafts(store.port, 4, 8).reason == "no_archive"
    store.chapter.segments[0].kind = "image"
    with pytest.raises(KeyError):
        read_precision_drafts(store.port, 4, 8)
    store.chapter.segments[0].kind = "text"
    store.chapter.segments[0].source = " "
    with pytest.raises(KeyError):
        read_precision_drafts(store.port, 4, 8)


def test_legacy_source_text_is_bound_to_fingerprint():
    store = Store()
    root = publish(store, legacy=True)
    store.chapter.segments[0].source = "changed"
    assert read_precision_drafts(store.port, 4, 8).reason == "source_mismatch"
    store.artifacts[f"{root}/inputs.json"]["plan"]["sources"][0] = "changed"
    assert read_precision_drafts(store.port, 4, 8).reason == "corrupt"
