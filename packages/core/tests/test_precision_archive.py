"""Offline precision archive contracts using an in-memory artifact port."""

import copy
import json
from dataclasses import asdict, replace

import pytest
from wenyi_core.agents import prompts
from wenyi_core.glossary.store import GlossaryTerm
from wenyi_core.storage.precision_archive import PrecisionArchive, safe_model_snapshot


class MemoryArtifacts:
    def __init__(self):
        self.data = {}

    def read_artifact(self, key):
        return copy.deepcopy(self.data.get(key))

    def write_artifact(self, key, value):
        self.data[key] = copy.deepcopy(value)

    def delete_artifact(self, key):
        self.data.pop(key, None)

    def list_artifacts(self, prefix=""):
        return sorted(key for key in self.data if key.startswith(prefix))

    def append_artifact_record(self, key, record):
        self.data.setdefault(key, []).append(copy.deepcopy(record))

    def read_artifact_records(self, key):
        return copy.deepcopy(self.data.get(key, []))


def test_objects_hash_conflicts_and_missing():
    store = MemoryArtifacts()
    archive = PrecisionArchive(store)
    ref = archive.put({"text": "source", "other": [1, None]})
    assert archive.put({"other": [1, None], "text": "source"}) == ref
    assert archive.get(ref)["text"] == "source"
    store.data[ref]["value"]["text"] = "corrupt"
    with pytest.raises(ValueError, match="hash mismatch"):
        archive.get(ref)
    with pytest.raises(ValueError, match="Conflicting"):
        archive.put({"text": "source", "other": [1, None]})
    assert store.data[ref]["value"]["text"] == "corrupt"
    del store.data[ref]
    with pytest.raises(ValueError, match="Missing"):
        archive.get(ref)
    with pytest.raises(ValueError, match="reference"):
        archive.get("../../private")
    null = archive.put(None)
    assert archive.get(null) is None


def test_glossary_delta_exact_metadata_order_and_history(monkeypatch):
    store = MemoryArtifacts()
    archive = PrecisionArchive(store)
    a = GlossaryTerm("a", "A", aliases=["alias"], note="raw note", first_chapter=3)
    b = GlossaryTerm("b", "B")
    first = archive.glossary((a,))
    added = archive.glossary((a, b))
    assert store.data[added]["appended"] == ["b"]
    assert "order" not in store.data[added]
    updated_a = replace(a, note="metadata only")
    updated = archive.glossary((updated_a, b))
    assert set(store.data[updated]["changed"]) == {"a"}
    reordered = archive.glossary((b, updated_a))
    assert store.data[reordered]["order"] == ["b", "a"]
    removed = archive.glossary((b,))
    assert store.data[removed]["removed"] == ["a"]
    assert archive.load_glossary(removed) == (b,)
    assert archive.load_glossary(reordered) == (b, updated_a)
    assert archive.glossary((a,)) == first
    text = archive.glossary_text(added)
    monkeypatch.setattr(prompts, "render_glossary", lambda terms: "NEW RENDERER")
    assert archive.glossary_text(added) == text
    assert archive.load_glossary(first) == (a,)
    store.data[added]["changed"]["b"] = store.data[first]["base"][0]
    with pytest.raises(ValueError):
        archive.load_glossary(added)


def test_compaction_and_snapshot_dedup():
    store = MemoryArtifacts()
    archive = PrecisionArchive(store)
    refs = []
    for i in range(70):
        terms = (GlossaryTerm("source", "target", note=str(i)),)
        refs.append(archive.glossary(terms))
    assert store.data[refs[32]]["depth"] == 0
    assert "base" in store.data[refs[32]]
    assert max(store.data[ref]["depth"] for ref in refs) <= 31
    for i, ref in enumerate(refs):
        assert archive.load_glossary(ref)[0].note == str(i)
    assert archive.glossary((GlossaryTerm("source", "target", note="1"),)) == refs[1]


def test_plan_and_messages_exact_replay_without_large_glossary_duplicates(monkeypatch):
    store = MemoryArtifacts()
    archive = PrecisionArchive(store)
    terms = tuple(GlossaryTerm(f"source{i}", "large target" * 100) for i in range(20))
    plan = {"terms": [asdict(term) for term in terms], "sources": ["source"], "flag": False}
    frozen = archive.freeze_plan(plan)
    assert archive.thaw_plan(frozen) == plan
    glossary_ref = frozen["glossary_ref"]
    glossary = archive.glossary_text(glossary_ref)
    recipes = []
    originals = []
    for i in range(4):
        packet = {"style": "style", "glossary": glossary, "sources": [f"source{i}"]}
        messages = [
            {"role": "system", "content": "historical template"},
            {
                "role": "user",
                "content": "prefix\n" + json.dumps(packet, ensure_ascii=False) + "\nsuffix",
            },
        ]
        originals.append(messages)
        recipes.append(archive.messages(messages, glossary_ref))
    assert any("glossary_ref" in part for part in recipes[0][1]["fields"]["content"])
    assert not any(
        isinstance(record.get("value"), str) and glossary in record["value"]
        for record in store.data.values()
    )
    monkeypatch.setattr(prompts, "render_glossary", lambda terms: "changed")
    for recipe, original in zip(recipes, originals):
        assert archive.load_messages(recipe) == original
    unusual = [{"role": "user", "content": '{"glossary" : ' + json.dumps(glossary) + "}"}]
    fallback = archive.messages(unusual, glossary_ref)
    assert archive.load_messages(fallback) == unusual
    assert len(fallback[0]["fields"]["content"]) == 1
    assert "literal" in fallback[0]["fields"]["content"][0]


def test_redaction_retains_inference_and_env_variable_names():
    result = safe_model_snapshot(
        {
            "api_key_env": "FAKE_ENV_VARIABLE",
            "api_key": "FAKE_KEY",
            "max_tokens": 1234,
            "temperature": 0.3,
            "prompt": {"token": "literal prompt text", "url": "https://u:p@example.invalid"},
            "headers": {"Authorization": "FAKE_HEADER"},
            "nested": {"password": "FAKE_PASSWORD", "access_token": "FAKE_TOKEN"},
            "base_url": "https://fake-user:fake-pass@example.invalid/api?api_key=FAKE_QUERY&mode=test",
        }
    )
    config = result["config"]
    assert config["api_key_env"] == "FAKE_ENV_VARIABLE"
    assert config["max_tokens"] == 1234
    assert config["temperature"] == 0.3
    assert config["prompt"] == {"url": "https://example.invalid"}
    assert config["nested"] == {}
    assert "fake-user" not in config["base_url"]
    assert "FAKE_QUERY" not in config["base_url"]
    assert "mode=test" in config["base_url"]
    assert {"api_key", "headers", "nested.password", "nested.access_token"} <= set(
        result["redacted"]
    )


def test_synthesis_recipes_reuse_draft_target_objects():
    store = MemoryArtifacts()
    archive = PrecisionArchive(store)
    glossary = archive.glossary(())
    drafts = [["large draft A" * 100], ["large draft B" * 100], ["large draft C" * 100]]
    target_refs = [archive.put(draft) for draft in drafts]
    messages = [
        {
            "role": "user",
            "content": "old task\n"
            + json.dumps({"segments": [0], "drafts": drafts}, ensure_ascii=False)
            + "\nend",
        }
    ]
    recipe = archive.messages(messages, glossary)
    parts = recipe[0]["fields"]["content"]
    assert next(part["draft_refs"] for part in parts if "draft_refs" in part) == target_refs
    assert archive.load_messages(recipe) == messages
    assert not any(
        isinstance(record.get("value"), str) and drafts[0][0] in record["value"]
        for record in store.data.values()
    )


@pytest.mark.parametrize("indent", [None, 2])
@pytest.mark.parametrize("ensure_ascii", [False, True])
def test_response_and_packet_json_fields_share_raw_objects(indent, ensure_ascii):
    store = MemoryArtifacts()
    archive = PrecisionArchive(store)
    terms = (GlossaryTerm("source", "target"),)
    plan = {"terms": [asdict(term) for term in terms], "sources": ["源文\ntext"]}
    frozen = archive.freeze_plan(plan)
    glossary_ref = frozen["glossary_ref"]
    targets = ["译文\n" * 100, "second target"]
    target_ref = archive.put(targets)
    values = [
        {"translations": targets},
        {
            "sources": plan["sources"],
            "glossary": archive.glossary_text(glossary_ref),
            "annotations": [{"target_key": "ref", "source": "note", "applies_to": [0]}],
        },
        {"drafts": [targets, ["second draft"]], "segments": [0, 1]},
    ]
    messages = [
        {
            "role": "assistant",
            "content": "prefix\n"
            + json.dumps(value, ensure_ascii=ensure_ascii, indent=indent)
            + "\nsuffix",
        }
        for value in values
    ]
    recipe = archive.messages(messages, glossary_ref)
    assert archive.load_messages(recipe) == messages
    assert (
        next(part["target_ref"] for part in recipe[0]["fields"]["content"] if "target_ref" in part)
        == target_ref
    )
    assert frozen["fields"]["sources"] in [
        part["json_ref"] for part in recipe[1]["fields"]["content"] if "json_ref" in part
    ]
    assert not any(
        isinstance(record.get("value"), str) and targets[0] in record["value"]
        for record in store.data.values()
    )
    store.data[target_ref]["value"][0] = "corrupt"
    with pytest.raises(ValueError, match="hash mismatch"):
        archive.load_messages(recipe)


def test_noncanonical_response_json_stays_literal():
    archive = PrecisionArchive(MemoryArtifacts())
    glossary_ref = archive.glossary(())
    messages = [{"role": "assistant", "content": '{"translations" : ["target"]}'}]
    recipe = archive.messages(messages, glossary_ref)
    assert set(recipe[0]["fields"]["content"][0]) == {"literal"}
    assert archive.load_messages(recipe) == messages


class CountingArtifacts(MemoryArtifacts):
    def __init__(self):
        super().__init__()
        self.reads = []

    def read_artifact(self, key):
        self.reads.append(key)
        return super().read_artifact(key)


def test_capture_read_counts_and_explicit_replay_revalidates():
    store = CountingArtifacts()
    archive = PrecisionArchive(store)
    terms = tuple(GlossaryTerm(str(i), "target") for i in range(200))
    ref = archive.glossary(terms)
    text = archive.glossary_text(ref)
    term_refs = set(store.data[ref]["base"])
    store.reads.clear()
    message = [{"role": "user", "content": json.dumps({"glossary": text, "sources": ["text"]})}]
    for _ in range(8):
        archive.messages(message, ref)
    assert not term_refs.intersection(store.reads)
    assert store.reads.count(ref) == 0
    assert len(store.reads) < 200
    # Raw-response capture on a fresh instance does not load unrelated terms.
    fresh = PrecisionArchive(store)
    store.reads.clear()
    fresh.messages([{"role": "assistant", "content": '{"translations": ["target"]}'}], ref)
    assert not term_refs.intersection(store.reads)
    assert store.reads.count(ref) == 1
    # Another revision checks its head without walking the accumulated chain.
    store.reads.clear()
    archive.glossary(terms + (GlossaryTerm("added", "new"),))
    assert store.reads.count(ref) <= 1
    assert sum(key in term_refs for key in store.reads) == len(terms)
    # Capture caches must not hide external corruption during explicit replay.
    bad_ref = next(iter(term_refs))
    store.data[bad_ref]["value"]["raw"]["note"] = "tampered"
    with pytest.raises(ValueError, match="hash mismatch"):
        archive.load_glossary(ref)
    with pytest.raises(ValueError, match="hash mismatch"):
        archive.glossary_text(ref)


def test_fresh_capture_uses_validated_head_index_not_revision_ancestry():
    store = CountingArtifacts()
    archive = PrecisionArchive(store)
    terms = tuple(GlossaryTerm(str(i), "target") for i in range(100))
    history = []
    for i in range(12):
        history.append(archive.glossary(terms + (GlossaryTerm("changing", str(i)),)))
    store.reads.clear()
    fresh = PrecisionArchive(store)
    fresh.glossary(terms + (GlossaryTerm("changing", "next"),))
    assert not set(history[:-1]).intersection(store.reads)
    assert store.reads.count(history[-1]) == 1
    assert len(store.reads) < 120


def test_config_prompt_named_subtrees_and_url_fragments_do_not_bypass_redaction():
    value = {
        "options": {
            "prompt": {"api_key": "FAKE_NESTED_KEY", "max_tokens": 500},
            "messages": [
                {
                    "password": "FAKE_NESTED_PASSWORD",
                    "endpoint": "https://FAKE_USER:FAKE_PASS@example.invalid/#access_token=FAKE_FRAGMENT",
                },
                {
                    "system_prompt": {
                        "headers": {"Authorization": "FAKE_AUTHORIZATION"},
                        "url": "https://example.invalid/#/route?token=FAKE_ROUTE_TOKEN&mode=demo",
                    }
                },
            ],
        },
    }
    result = safe_model_snapshot(value)
    serialized = json.dumps(result)
    assert "FAKE_" not in serialized
    assert result["config"]["options"]["prompt"] == {"max_tokens": 500}
    assert "options.prompt.api_key" in result["redacted"]
    assert "options.messages[0].endpoint.fragment.access_token" in result["redacted"]
    assert "options.messages[1].system_prompt.url.fragment.token" in result["redacted"]
    assert "mode=demo" in serialized
    # Prompt prose follows the archive path, not the configuration sanitizer.
    archive = PrecisionArchive(MemoryArtifacts())
    messages = [{"role": "user", "content": "literal FAKE_TOKEN https://u:p@example.invalid"}]
    recipe = archive.messages(messages, archive.glossary(()))
    assert archive.load_messages(recipe) == messages


def test_explicit_deep_restore_reads_each_term_once_and_revalidates_next_time():
    store = CountingArtifacts()
    archive = PrecisionArchive(store)
    terms = tuple(GlossaryTerm(str(i), "target") for i in range(200))
    refs = []
    for i in range(31):
        refs.append(archive.glossary(terms + (GlossaryTerm("changing", str(i)),)))
    assert store.data[refs[-1]]["depth"] == 30
    store.reads.clear()
    assert archive.load_glossary(refs[-1])[-1].target == "30"
    assert len(store.reads) <= len(terms) + 31 + 31
    assert len(store.reads) == len(set(store.reads))
    common_ref = store.data[refs[0]]["base"][0]
    store.data[common_ref]["value"]["raw"]["note"] = "external mutation"
    with pytest.raises(ValueError, match="hash mismatch"):
        archive.load_glossary(refs[-1])
