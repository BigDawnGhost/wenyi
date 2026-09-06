"""Offline multilingual translation, resource rendering and target-isolation regressions."""

from __future__ import annotations

import json
import re
import tempfile
import zipfile
from itertools import permutations
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from tests.fake_llm import routing_handler
from trans_novel.agents import prompts
from trans_novel.assemble.writer import assemble
from trans_novel.assemble.writer_common import _epub_lang
from trans_novel.cli import app
from trans_novel.config import Config
from trans_novel.i18n.languages import profile, supported_languages, validate_run_languages
from trans_novel.i18n.prompts import template
from trans_novel.i18n.resources import prompt_fingerprint, read_text
from trans_novel.llm.providers.fake import FakeClient
from trans_novel.pipeline.language import normalize_lang
from trans_novel.pipeline.orchestrator import Orchestrator
from trans_novel.pipeline.runstore import RunStore, translation_run_dir
from trans_novel.srt.translate import translate_srt


def test_non_chinese_translation_has_no_chinese_target_instruction():
    system = prompts.render("translator_system", src="zh", tgt="en")
    assert "into English" in system
    assert "简体中文" not in system
    assert "按中文" not in system


def test_language_tags_preserve_script_and_region():
    assert normalize_lang("zh-Hant") == "zh-Hant"
    assert normalize_lang("en_US") == "en-US"
    assert normalize_lang("not-a-language") == ""
    assert _epub_lang("en_US") == "en-US"
    assert _epub_lang("zh-TW") == "zh-Hant"


def test_unknown_target_is_rejected_before_work():
    with pytest.raises(ValueError, match="Unsupported"):
        Config.from_dict({"language": {"target": "not-a-language"}})


@pytest.mark.parametrize("target_order", [("zh", "ja"), ("ja", "zh")])
def test_book_targets_have_independent_state_and_output(target_order):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "book.txt"
        source.write_text("# Chapter one\n\nThe door opened.\n", encoding="utf-8")
        stores = []
        outputs = []
        for target in target_order:
            config = Config.from_dict(
                {
                    "language": {"source": "en", "target": target},
                    "paths": {"state_dir": str(root / "state")},
                    "pipeline": {"book_understanding": False},
                }
            )
            client = FakeClient(handler=routing_handler)
            store = Orchestrator(config, client=client).prepare(str(source))
            assert store.load_manifest()["target_lang"] == target
            stores.append(store.run_dir)
            output = assemble(store, str(source), out_format="txt", about_page=False)
            assert output.endswith(f".{target}.txt")
            outputs.append(output)
        assert stores[0] != stores[1]
        assert outputs[0] != outputs[1]
        for target, run_dir in zip(target_order, stores):
            assert translation_run_dir(str(root / "state"), "book", target) == run_dir


@pytest.mark.parametrize("source,target", list(permutations(("zh", "en", "ja"), 2)))
def test_direct_translation_polishing_review_and_resume(source, target):
    """Exercise six directions through services, persistence, export and resume; Fake does not
    prove quality.
    """
    translated = {
        "zh": "门打开了。",
        "en": 'The door opened. "Hello!"',
        "ja": "扉が開いた。「こんにちは！」",
    }[target]
    target_label = profile(target)["label"]

    def handler(messages, tier, json_mode):
        system, user = messages[0]["content"], messages[-1]["content"]
        n = len(re.findall(r"^\[\d+\]", user, re.MULTILINE))
        if "pre-translation analyst" in system:
            assert f"Suggested name in {target_label}" in system
            return json.dumps(
                {"style_guide": "Keep the narrator's voice.", "characters": [], "terms": []}
            )
        if "literary translator" in system:
            assert f"into {target_label}" in system
            assert profile(source)["label"] in system
            return json.dumps({"translations": [translated] * n})
        if "prose editor" in system:
            assert f"{target_label} prose editor" in system
            return json.dumps({"polished": [translated] * n})
        if "chapter title translator" in system:
            assert f"into {target_label}" in system
            return json.dumps({"titles": [translated] * n})
        if "chapter digest writer" in system or "whole-book synopsis writer" in system:
            assert f"write in {target_label}" in system
            return translated
        if "extractor" in system:
            assert f"actual {target_label} wording" in system
            return '{"terms":[]}'
        return routing_handler(messages, tier, json_mode)

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        path = root / "book.txt"
        original = {"zh": "门打开了。", "en": "The door opened.", "ja": "扉が開いた。"}[source]
        path.write_text(f"# One\n\n{original}\n", encoding="utf-8")
        config = Config.from_dict(
            {
                "language": {"source": source, "target": target},
                "paths": {"state_dir": str(root / "state")},
                "pipeline": {
                    "review_agent_loop": False,
                    "review_fix_loop": False,
                    "review_autofix": False,
                },
            }
        )
        client = FakeClient(handler=handler)
        orchestrator = Orchestrator(config, client=client)
        store = orchestrator.run(str(path))
        formal_before = Path(store.chapter_path(0)).read_bytes()
        assert store.load_chapter(0).text_segments[0].target == translated
        assert store.load_manifest()["prompt_fingerprint"] == prompt_fingerprint()
        orchestrator.run_review(str(path))
        stages = {call["stage"] for call in client.calls}
        assert {"Analyzer", "Translator", "Polisher", "Synopsizer", "GlossaryExtractor"} <= stages
        for fmt in ("txt", "markdown", "html", "epub", "docx"):
            for bilingual in (False, True):
                output = Path(
                    assemble(
                        store,
                        str(path),
                        out_format=fmt,
                        bilingual=bilingual,
                        punctuation_normalize=True,
                    )
                )
                assert f".{target}{'-bi' if bilingual else ''}." in output.name
                assert output.is_file()
                if fmt == "txt":
                    assert translated in output.read_text(encoding="utf-8")
                if fmt == "epub":
                    with zipfile.ZipFile(output) as book:
                        opf = next(name for name in book.namelist() if name.endswith(".opf"))
                        assert (
                            f">{'zh-Hans' if target == 'zh' else target}</"
                            in book.read(opf).decode()
                        )
        explicit = root / "custom.txt"
        assert assemble(store, str(path), str(explicit), out_format="txt") == str(explicit)
        assert Path(store.chapter_path(0)).read_bytes() == formal_before
        assert path.read_text(encoding="utf-8") == f"# One\n\n{original}\n"
        calls_before = len(client.calls)
        orchestrator.run(str(path))
        orchestrator.run_review(str(path))
        assert len(client.calls) == calls_before


@pytest.mark.parametrize("target", supported_languages())
def test_every_profile_renders_all_tasks_without_missing_fields(target):
    root = Path(__file__).resolve().parents[1] / "trans_novel/i18n/data/tasks"
    for path in root.glob("*.txt"):
        name = path.stem
        variables = {}
        for match in template(name).pattern.finditer(template(name).template):
            key = match.group("named") or match.group("braced")
            if key:
                variables[key] = "fixture"
        rendered = prompts.render(name, src="en", tgt=target, **variables)
        assert "$tgt_label" not in rendered
    for name in (
        "translator_system",
        "polisher_system",
        "title_translator_system",
        "analyzer_system",
        "glossary_extractor_system",
        "chapter_digest_system",
        "book_synopsis_system",
    ):
        rendered = prompts.render(name, src="ja", tgt=target)
        if target not in {"zh", "zh-Hant"}:
            assert "简体中文" not in rendered
            assert "中文译" not in rendered


def test_strict_template_and_literal_source_payload():
    with pytest.raises(ValueError, match="missing argument"):
        prompts.render("translator_user", src="en", tgt="ja")
    source = '${tgt_label} $n {"translations": []}'
    assert source in prompts.render("chapter_digest_user", source=source)
    with pytest.raises(ValueError, match="Unsupported"):
        prompts.render("translator_system", src="en", tgt="zz")
    with pytest.raises(ValueError):
        read_text("../config.yaml")


def test_legacy_non_chinese_state_is_preserved_and_languages_are_checked():
    with tempfile.TemporaryDirectory() as directory:
        legacy = RunStore(str(Path(directory) / "book"))
        legacy.save_manifest({"source_lang": "ja", "target_lang": "en"})
        before = Path(legacy.manifest_path).read_bytes()
        assert translation_run_dir(directory, "book", "en") == legacy.run_dir
        assert translation_run_dir(directory, "book", "zh") == str(
            Path(legacy.run_dir) / "targets/zh"
        )
        for source, target in (("en", "en"), ("auto", "zh")):
            with pytest.raises(ValueError, match="does not match"):
                validate_run_languages(legacy.load_manifest(), source, target)
        assert Path(legacy.manifest_path).read_bytes() == before


def test_srt_target_isolation_traditional_script_and_resume():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "captions.srt"
        source.write_text("7\n00:00:01,000 --> 00:00:02,000\nThe door opened.\n", encoding="utf-8")
        results = []
        for target, text in (
            ("zh", "门打开了。"),
            ("zh-Hant", "「門打開了！」"),
            ("ja", "扉が開いた。"),
        ):

            def handler(messages, tier, json_mode):
                assert profile(target)["english_name"] in messages[0]["content"]
                if target == "zh-Hant":
                    assert "Use Traditional Chinese" in messages[0]["content"]
                return json.dumps({"7": text})

            config = Config.from_dict(
                {
                    "language": {"source": "en", "target": target},
                    "paths": {"state_dir": str(root / "state")},
                    "output": {"bilingual": True},
                }
            )
            client = FakeClient(handler=handler)
            result = translate_srt(str(source), config, client=client)
            for output in result["outputs"]:
                assert text in Path(output).read_text(encoding="utf-8")
                assert "00:00:01,000 --> 00:00:02,000" in Path(output).read_text(encoding="utf-8")
                assert f".{target}" in Path(output).name
            calls_before = len(client.calls)
            translate_srt(str(source), config, client=client)
            assert len(client.calls) == calls_before
            results.append(result["run_dir"])
        assert len(set(results)) == 3


def test_language_list_needs_no_api_and_reads_packaged_templates():
    runner = CliRunner()
    with (
        tempfile.TemporaryDirectory() as directory,
        patch("trans_novel.cli._validate_api_configuration") as validate,
    ):
        result = runner.invoke(app, ["--config", str(Path(directory) / "config.yaml"), "languages"])
        assert result.exit_code == 0, result.output
        assert "zh-Hant" in result.output
        assert "en-GB" in result.output
        validate.assert_not_called()


@pytest.mark.parametrize("command", ["translate", "status", "report", "assemble"])
@pytest.mark.parametrize("invalid", ["language: {target: typo}\n", "language: [\n"])
def test_cli_invalid_language_or_yaml_is_a_concise_error(command, invalid):
    runner = CliRunner()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "config.yaml"
        path.write_text(invalid, encoding="utf-8")
        result = runner.invoke(app, ["--config", str(path), command, "missing.txt"])
        assert result.exit_code == 1, result.output
        assert "Configuration error" in result.output
        assert "Error: 1" not in result.output
        assert "Traceback" not in result.output


def test_partial_non_chinese_run_resumes_without_retranslating_saved_batch():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "novel.txt"
        source.write_text("第一段已经完成。\n\n第二段在中断后继续。\n", encoding="utf-8")
        config = Config.from_dict(
            {
                "language": {"source": "zh", "target": "en"},
                "paths": {"state_dir": str(root / "state")},
                "segment": {"max_chars_per_batch": 10},
                "pipeline": {"polish": False, "book_understanding": False},
            }
        )
        translated_calls = 0

        def handler(messages, tier, json_mode):
            nonlocal translated_calls
            system = messages[0]["content"]
            if "literary translator" in system:
                translated_calls += 1
                if translated_calls == 2:
                    raise RuntimeError("synthetic interruption")
                return '{"translations":["The first paragraph is complete."]}'
            if "extractor" in system:
                return '{"terms":[]}'
            return routing_handler(messages, tier, json_mode)

        first = Orchestrator(config, client=FakeClient(handler=handler))
        with pytest.raises(RuntimeError, match="synthetic interruption"):
            first.run(str(source))

        def resume_handler(messages, tier, json_mode):
            if "literary translator" in messages[0]["content"]:
                pending = messages[-1]["content"].split(
                    "[Simplified Chinese paragraphs to translate]", 1
                )[1]
                assert "第一段已经完成" not in pending
                assert "第二段" in pending
                return '{"translations":["The second paragraph resumes."]}'
            if "extractor" in messages[0]["content"]:
                return '{"terms":[]}'
            return routing_handler(messages, tier, json_mode)

        resumed = Orchestrator(config, client=FakeClient(handler=resume_handler)).run(str(source))
        assert [s.target for s in resumed.load_chapter(0).text_segments] == [
            "The first paragraph is complete.",
            "The second paragraph resumes.",
        ]


def test_review_rechecks_when_language_resources_change():
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "novel.txt"
        source.write_text("# Start\n\n扉が開いた。\n", encoding="utf-8")
        config = Config.from_dict(
            {
                "language": {"source": "ja", "target": "en"},
                "paths": {"state_dir": str(Path(directory) / "state")},
                "pipeline": {
                    "polish": False,
                    "book_understanding": False,
                    "review_fix_loop": False,
                    "review_agent_loop": False,
                    "review_autofix": False,
                },
            }
        )
        client = FakeClient(handler=routing_handler)
        orchestrator = Orchestrator(config, client=client)
        orchestrator.run(str(source))
        orchestrator.run_review(str(source))
        before = len(client.calls)
        orchestrator.run_review(str(source))
        assert len(client.calls) == before
        with patch(
            "trans_novel.pipeline.review_workflow.prompt_fingerprint", return_value="new-resources"
        ):
            orchestrator.run_review(str(source))
        assert len(client.calls) > before
