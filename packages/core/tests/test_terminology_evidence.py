"""Source indexing must cover every match without borrowing translated text."""

from wenyi_core.glossary.evidence import BookSourceIndex
from wenyi_core.glossary.store import GlossaryTerm
from wenyi_core.ingest.models import Chapter, Segment


def index(*sources):
    return BookSourceIndex(
        [Chapter(index=3, segments=[Segment(index=i, source=s) for i, s in enumerate(sources)])]
    )


def test_source_boundaries_and_no_target_evidence():
    book = BookSourceIndex(
        [Chapter(index=0, segments=[Segment(index=0, source="Anna", target="Ann")])]
    )
    assert book.evidence_groups(GlossaryTerm("Ann", "安"), []) == []
    assert index("アンナ").evidence_groups(GlossaryTerm("アン", "安"), [])


def test_all_matches_neighbors_deduplicated_and_grouped():
    book = index(*[f"Ann at {i}" for i in range(20)])
    groups = book.evidence_groups(GlossaryTerm("Ann", "安"), [], budget=30)
    passages = [p for group in groups for p in group]
    assert {p.segment for p in passages} == set(range(20))
    assert len({p.reference for p in passages}) == len(passages)
    assert all(sum(len(p.source) for p in group) <= 30 for group in groups)
    assert index("before", "Ann", "after").evidence_groups(GlossaryTerm("Ann", "安"), [])[0]
    assert (
        len(index("before", "Ann", "after").evidence_groups(GlossaryTerm("Ann", "安"), [])[0]) == 3
    )


def test_long_source_offsets_and_fingerprint_ignore_translation():
    source = "Ann " + "あいうえお \n" * 100
    book = index(source)
    groups = book.evidence_groups(GlossaryTerm("Ann", "安"), [], budget=21)
    passages = [p for group in groups for p in group]
    assert "".join(p.source for p in passages) == source
    assert [p.start for p in passages] == list(range(0, len(source), 21))
    assert all(source[p.start : p.start + len(p.source)] == p.source for p in passages)
    assert book.evidence_groups(GlossaryTerm("Ann", "安"), [], budget=21) == groups
    assert book.fingerprint == index(source).fingerprint
    assert book.fingerprint != index(source + "x").fingerprint
    assert book.for_segments(3, [Segment(index=0, source=source, target="translated")]) == (
        book.passages
    )


def test_alias_conflict_and_untrusted_alias():
    ann = GlossaryTerm("Ann", "安", aliases=["Ace"])
    assert index("Ace").evidence_groups(ann, [ann])
    other = GlossaryTerm("Bob", "鲍勃", aliases=["ＡＣＥ"])
    assert index("Ace").evidence_groups(ann, [ann, other]) == []
    assert index("Ace").evidence_groups(ann, [GlossaryTerm("Ace", "另一个")]) == []
    ann.status = "conflict"
    assert index("Ace").evidence_groups(ann, [ann]) == []


def test_empty_and_invalid_budget():
    import pytest

    book = index("Ann")
    with pytest.raises(ValueError, match="budget"):
        book.evidence_groups(GlossaryTerm("Ann", "安"), [], budget=0)
    assert book.for_segments(3, []) == []
