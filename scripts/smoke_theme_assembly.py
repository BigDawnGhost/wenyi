from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import zipfile
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import yaml
from lxml import etree

from tests.fixtures.books import write_nested_toc_epub, write_sample_txt
from tests.fixtures.fake_llm import fake_llm_dict, routing_handler
from trans_novel.benchmark.epub_check import validate_epub_triplet
from trans_novel.config import Config
from trans_novel.llm import FakeClient
from trans_novel.pipeline import Application


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _targets(run_dir: Path) -> list[list[Any]]:
    chapter_paths = sorted((run_dir / "chapters_v2").glob("*.json"))
    if not chapter_paths:
        raise AssertionError("completed run has no persisted chapters")
    result = []
    for path in chapter_paths:
        chapter = json.loads(path.read_text(encoding="utf-8"))
        segments = chapter.get("segments")
        if not isinstance(segments, list):
            raise AssertionError(f"invalid persisted chapter: {path}")
        result.append([segment["target"] for segment in segments])
    return result


_LAYOUT_BODY = """<?xml version="1.0" encoding="UTF-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>Layout smoke</title><style>.block { margin: 1em; } .right { text-align: right; } .footnote { font-size: .9em; }</style></head><body>
<h1 id="part-1">PART I</h1><p id="body-copy">Opening body with a standard note <a id="noteref-1" epub:type="noteref" role="doc-noteref" href="#footnote-1">1</a>.</p>
<div class="block" id="quote-container"><p id="quote-text">Quoted source text.</p><p class="right" id="quote-attribution">— Source Author</p></div>
<h2 id="section-1">Section 1</h2><div class="block" id="summary-container"><p id="chapter-summary">Chapter summary source.</p></div>
<p class="footnote" id="bibliography">Bibliography: Reused footnote class.</p>
<aside id="footnote-1" epub:type="footnote" role="doc-footnote"><p class="footnote" id="footnote-text">Standard footnote source. <a id="backlink-1" href="#noteref-1">↩</a></p></aside>
<p class="block" id="unknown-block">Unclassified ornamental line.</p><h1 id="part-2">PART II</h1><p id="part-2-body">Part II body.</p><h2 id="section-2">Section 2</h2><p id="section-2-body">Section 2 body.</p>
</body></html>
"""

_FIXTURE_ROLES = {
    "quote-text": ("quote-text", None),
    "quote-attribution": ("quote-attribution", None),
    "chapter-summary": ("chapter-summary", None),
    "noteref-1": ("noteref", None),
    "footnote-text": ("footnote", None),
    "bibliography": ("body", None),
    "unknown-block": (None, None),
}


def _write_layout_epub(path: Path) -> None:
    write_nested_toc_epub(str(path), toc_kind="nav")
    temporary = path.with_suffix(".tmp.epub")
    with zipfile.ZipFile(path, "r") as source, zipfile.ZipFile(temporary, "w") as output:
        for info in source.infolist():
            data = (
                _LAYOUT_BODY.encode() if info.filename == "OEBPS/body.xhtml" else source.read(info)
            )
            output.writestr(info, data)
    os.replace(temporary, path)


def _config(case_dir: Path, *, source_lang: str) -> tuple[Config, dict[str, Any]]:
    raw = {
        "llm": fake_llm_dict(),
        "output": {
            "mono": True,
            "bilingual": {"enabled": True, "order": "target_first"},
            "override_theme": {
                "styles": "builtin:chinese-reading",
            },
            "bilingual_styles": "builtin:bilingual",
        },
    }
    config = Config.from_dict(raw)
    config.source_lang = source_lang
    config.target_lang = "zh"
    config.state_dir = str(case_dir / "state")
    strict_yaml = {
        "llm": config.llm.model_dump(mode="json"),
        "quality": config.quality,
        "output": config.output.model_dump(mode="json"),
    }
    return config, strict_yaml


_GOLD_TRANSLATIONS = {
    "PART I": "第一部",
    "Opening body with a standard note .": "正文开篇，其中包含一个标准脚注。",
    "Quoted source text.": "这是一段用于验证排版的引文正文。",
    "— Source Author": "——原作者",
    "Section 1": "第一节",
    "Chapter summary source.": "这是与引文共用容器类名的章节提要。",
    "Bibliography: Reused footnote class.": "参考文献：此条目复用了脚注类名，但它不是脚注。",
    "Standard footnote source. ↩": "这是标准脚注的正文。↩",
    "Unclassified ornamental line.": "这是模型明确标记为未知的装饰段落。",
    "PART II": "第二部",
    "Part II body.": "第二部正文。",
    "Section 2": "第二节",
    "Section 2 body.": "第二节正文。",
}

_GOLD_HEADINGS = {
    "part-1": "第一部",
    "section-1": "第一节",
    "part-2": "第二部",
    "section-2": "第二节",
}


def _gold_translation(source: str) -> str | None:
    if source in _GOLD_TRANSLATIONS:
        return _GOLD_TRANSLATIONS[source]
    return source if re.fullmatch(r"(?:\d+|↩)", source) else None


def _smoke_translation_handler(messages, agent, operation, json_mode):
    system = messages[0]["content"]
    user = messages[-1]["content"]
    if not json_mode and (
        operation in {"title.translate", "translate.heading"} or "标题翻译" in system
    ):
        _prefix, marker, remainder = user.partition("【待译章节标题】\n")
        source, suffix, trailer = remainder.partition("\n\n请只输出标题译文。")
        if not marker or not suffix or trailer.strip() or not source.strip():
            raise AssertionError("unexpected plain title fixture request")
        source = source.strip()
        return _gold_translation(source) or f"中文标题：{source}"
    if operation == "title.translate":
        numbered = re.findall(r"^\[(\d+)\] (.*)$", user, re.M)
        if not numbered or [int(index) for index, _title in numbered] != list(range(len(numbered))):
            raise AssertionError("unexpected title fixture request")
        return json.dumps(
            {
                "titles": [
                    _gold_translation(title) or f"中文标题：{title}" for _index, title in numbered
                ]
            },
            ensure_ascii=False,
        )
    if operation == "translate.batch":
        sources = re.findall(r"^\[\d+\]\s*(.*)$", user.split("【待译", 1)[-1], re.M)
        translated = [_gold_translation(source) for source in sources]
        if sources and all(value is not None for value in translated):
            return json.dumps({"translations": translated}, ensure_ascii=False)
    if operation == "translate.single":
        source = user.rsplit("】", 1)[-1].strip()
        translated = _gold_translation(source)
        if translated is not None:
            return translated
    return routing_handler(messages, agent, operation, json_mode)


def _fixture_role(sample: dict[str, Any]) -> tuple[str | None, int | None]:
    markup = sample.get("source_markup")
    match = re.match(r'\s*<[^>]*\bid="([^"]+)"', markup) if isinstance(markup, str) else None
    if match and match.group(1) in _FIXTURE_ROLES:
        return _FIXTURE_ROLES[match.group(1)]
    features = sample.get("features")
    tag = features.get("tag") if isinstance(features, dict) else None
    if isinstance(tag, str) and len(tag) == 2 and tag[0] == "h" and tag[1] in "123456":
        return "heading", int(tag[1])
    return "body", None


_FIXTURE_API_KEY = "wenyi-layout-smoke"
_FIXTURE_MODEL = "layout-smoke"


class _LayoutFixtureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        record = {"path": self.path, "layout": False, "authorized": False}
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            messages = body["messages"]
            request = json.loads(messages[-1]["content"])
            samples = request["samples"]
            node_ids = [sample["node_id"] for sample in samples]
            record["authorized"] = self.headers.get("Authorization") == f"Bearer {_FIXTURE_API_KEY}"
            record["layout"] = (
                self.path == "/v1/chat/completions"
                and body.get("model") == _FIXTURE_MODEL
                and isinstance(messages, list)
                and "classify ORIGINAL book layout evidence" in messages[0]["content"]
                and isinstance(request.get("allowed_roles"), list)
                and "body" in request["allowed_roles"]
                and bool(node_ids)
                and len(node_ids) == len(set(node_ids))
                and all(isinstance(node_id, str) and node_id for node_id in node_ids)
            )
            if not record["authorized"] or not record["layout"]:
                raise ValueError("unexpected fixture request")
            content = json.dumps(
                {
                    "observations": [
                        {
                            "node_id": sample["node_id"],
                            "role": _fixture_role(sample)[0],
                            "level": _fixture_role(sample)[1],
                        }
                        for sample in samples
                    ],
                },
                separators=(",", ":"),
            )
            response = {
                "id": "layout-smoke",
                "object": "chat.completion",
                "created": 0,
                "model": _FIXTURE_MODEL,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        except (KeyError, TypeError, ValueError):
            self.server.requests.append(record)
            self._send(400, {"error": {"message": "unexpected fixture request"}})
            return
        self.server.requests.append(record)
        self._send(200, response)

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        return


@contextmanager
def _layout_fixture_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LayoutFixtureHandler)
    server.daemon_threads = True
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _run_frozen(
    binary: Path,
    config_path: Path,
    source: Path,
    output: Path,
    cwd: Path,
) -> None:
    subprocess.run(
        [
            str(binary),
            "--config",
            str(config_path),
            "tools",
            "assemble",
            str(source.resolve()),
            "--format",
            "epub",
            "--out",
            str(output),
        ],
        check=True,
        timeout=120,
        cwd=cwd,
        env={
            **os.environ,
            "PATH": "",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "WENYI_SMOKE_API_KEY": _FIXTURE_API_KEY,
        },
    )


def _epub_body(path: Path) -> etree._Element:
    with zipfile.ZipFile(path) as archive:
        return etree.fromstring(archive.read("OEBPS/body.xhtml"))


def _node_by_id(root: etree._Element, node_id: str) -> etree._Element:
    matches = root.xpath("//*[@id=$node_id]", node_id=node_id)
    if len(matches) != 1:
        raise AssertionError(f"expected one #{node_id}, found {len(matches)}")
    return matches[0]


def _assert_layout_surface(path: Path, *, bilingual: bool) -> dict[str, Any]:
    root = _epub_body(path)
    for node_id, (role, level) in _FIXTURE_ROLES.items():
        node = _node_by_id(root, node_id)
        if node.get("data-tn-role") != role:
            raise AssertionError(f"{path.name} #{node_id} role mismatch")
        if node.get("data-tn-level") != (str(level) if level is not None else None):
            raise AssertionError(f"{path.name} #{node_id} level mismatch")
        if role is not None and node.get("data-tn-content") != "target":
            raise AssertionError(f"{path.name} #{node_id} is not bound to target content")
        if not "".join(node.itertext()).strip():
            raise AssertionError(f"{path.name} #{node_id} lost its text")

    for node_id, expected in _GOLD_HEADINGS.items():
        actual = "".join(_node_by_id(root, node_id).itertext()).strip()
        if actual != expected:
            raise AssertionError(
                f"{path.name} #{node_id} title mismatch: {actual!r} != {expected!r}"
            )

    noteref = _node_by_id(root, "noteref-1")
    backlink = _node_by_id(root, "backlink-1")
    if noteref.get("href") != "#footnote-1" or "".join(noteref.itertext()).strip() != "1":
        raise AssertionError(f"{path.name} changed the footnote reference")
    if backlink.get("href") != "#noteref-1" or "".join(backlink.itertext()).strip() != "↩":
        raise AssertionError(f"{path.name} changed the footnote backlink")

    source_phrases = (
        "Quoted source text.",
        "— Source Author",
        "Chapter summary source.",
        "Standard footnote source.",
        "Bibliography: Reused footnote class.",
    )
    text = "".join(root.itertext())
    if bilingual and any(phrase not in text for phrase in source_phrases):
        raise AssertionError(f"{path.name} lost bilingual source text")
    return {
        "name": path.name,
        "roles": {node_id: role for node_id, (role, _level) in _FIXTURE_ROLES.items()},
        "footnote_links_preserved": True,
        "source_text_preserved": bilingual,
    }


def _assert_receipts(
    report: dict[str, Any],
    outputs: list[Path],
    source_sha256: str | None,
    expected_roles: tuple[str, ...],
) -> tuple[str, list[dict[str, Any]]]:
    digest = report.get("output_digest")
    if not isinstance(digest, str) or not digest:
        raise AssertionError("publication report has no output digest")
    if report.get("passed") is not True or report.get("published") is not True:
        raise AssertionError("publication report did not pass and publish")
    published = report.get("published_outputs")
    if not isinstance(published, dict):
        raise AssertionError("publication report has no physical receipts")

    evidence = []
    for output in outputs:
        if not output.is_file():
            raise AssertionError(f"missing assembled output: {output}")
        receipt = published.get(output.name)
        if not isinstance(receipt, dict):
            raise AssertionError(f"missing physical receipt: {output.name}")
        output_sha256 = _sha256(output)
        if (
            receipt.get("passed") is not True
            or receipt.get("published") is not True
            or receipt.get("output_sha256") != output_sha256
            or receipt.get("source_sha256") != source_sha256
            or receipt.get("output_digest") != digest
        ):
            raise AssertionError(f"invalid physical receipt: {output.name}")
        theme = receipt.get("theme")
        role_counts = theme.get("role_counts") if isinstance(theme, dict) else None
        resources = theme.get("resources") if isinstance(theme, dict) else None
        if not isinstance(role_counts, dict) or any(
            role_counts.get(role, 0) <= 0 for role in expected_roles
        ):
            raise AssertionError(f"missing theme role evidence: {output.name}")
        if not isinstance(resources, int) or resources <= 0:
            raise AssertionError(f"missing themed resources: {output.name}")
        evidence.append(
            {
                "name": output.name,
                "sha256": output_sha256,
                "roles": {role: role_counts[role] for role in expected_roles},
                "resources": resources,
            }
        )
    return digest, evidence


def _output_evidence(
    store,
    kind: str,
    source: Path,
    mono: Path,
    bilingual: Path,
    source_sha256: str,
) -> dict[str, Any]:
    report = store.load_epub_verification()
    if not isinstance(report, dict):
        raise AssertionError("assembly did not persist a publication report")
    expected_source = source_sha256 if kind == "source_epub" else None
    expected_roles = (
        ("body", "quote-text", "quote-attribution", "chapter-summary", "noteref", "footnote")
        if kind == "source_epub"
        else ("body",)
    )
    digest, outputs = _assert_receipts(report, [mono, bilingual], expected_source, expected_roles)
    if kind != "source_epub":
        if report.get("triplet") is not None:
            raise AssertionError("generated EPUB report must not contain a source triplet")
        return {
            "output_digest": digest,
            "outputs": outputs,
            "triplet_structural_pass": None,
            "layout_surface": None,
        }
    surfaces = [
        _assert_layout_surface(mono, bilingual=False),
        _assert_layout_surface(bilingual, bilingual=True),
    ]
    triplet = validate_epub_triplet(
        source,
        mono,
        bilingual,
        publication_report=report,
        output_digest=digest,
    )
    if triplet.get("structural_pass") is not True:
        raise AssertionError(f"source EPUB triplet failed: {triplet}")
    return {
        "output_digest": digest,
        "outputs": outputs,
        "triplet_structural_pass": True,
        "layout_surface": surfaces,
    }


def _run_case(binary: Path, root: Path, kind: str) -> dict[str, Any]:
    case_dir = root / kind
    case_dir.mkdir()
    source = case_dir / ("source.epub" if kind == "source_epub" else "source.txt")
    if kind == "source_epub":
        _write_layout_epub(source)
    else:
        write_sample_txt(str(source))
    source_before = _sha256(source)

    config, strict_yaml = _config(case_dir, source_lang="en" if kind == "source_epub" else "ja")
    result = Application(config, client=FakeClient(handler=_smoke_translation_handler)).run_all(
        str(source), out_format="txt"
    )
    store = result["store"]
    run_dir = Path(store.run_dir)
    targets_before = _targets(run_dir)
    usage_path = Path(store.usage_path)
    profile_path = Path(store.layout_profile_path)
    if not usage_path.is_file():
        raise AssertionError("completed run has no usage.json")
    if profile_path.exists():
        raise AssertionError("TXT run unexpectedly created a layout profile")

    config_path = (case_dir / "config.yaml").resolve()
    warm_mono = (case_dir / "warm.epub").resolve()
    warm_bilingual = (case_dir / "warm-bi.epub").resolve()
    mono = (case_dir / "output.epub").resolve()
    bilingual = (case_dir / "output-bi.epub").resolve()

    with _layout_fixture_server() as server:
        roles = strict_yaml["llm"]["models"]
        strict_yaml["llm"] = {
            "models": {role: [f"openai-compatible/{_FIXTURE_MODEL}"] for role in roles},
            "provider_routing": {},
            "base_url": f"http://127.0.0.1:{server.server_address[1]}/v1",
            "api_key_env": "WENYI_SMOKE_API_KEY",
        }
        config_path.write_text(
            yaml.safe_dump(strict_yaml, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )

        _run_frozen(binary, config_path, source, warm_mono, case_dir)
        first_request_count = len(server.requests)
        if first_request_count <= 0 or any(
            not request["layout"] or not request["authorized"] for request in server.requests
        ):
            raise AssertionError(f"frozen assembly made non-layout requests: {server.requests}")
        if not profile_path.is_file():
            raise AssertionError("frozen assembly did not create the missing layout profile")
        if not warm_mono.is_file() or not warm_bilingual.is_file():
            raise AssertionError("frozen automatic layout assembly did not produce both outputs")

        usage_after_analysis = usage_path.read_bytes()
        _run_frozen(binary, config_path, source, mono, case_dir)
        if len(server.requests) != first_request_count:
            raise AssertionError("frozen cached assembly made an additional model request")
        if usage_path.read_bytes() != usage_after_analysis:
            raise AssertionError("frozen cached assembly changed usage.json")

    source_after = _sha256(source)
    targets_after = _targets(run_dir)
    if source_after != source_before:
        raise AssertionError("assembly changed the source")
    if targets_after != targets_before:
        raise AssertionError("assembly changed persisted targets")

    evidence = _output_evidence(store, kind, source, mono, bilingual, source_before)

    return {
        "kind": kind,
        "source_sha256": source_before,
        **evidence,
        "source_unchanged": True,
        "targets_unchanged": True,
        "layout_model_calls": first_request_count,
        "cached_model_calls": 0,
        "translation_model_calls": 0,
        "usage_unchanged_after_analysis": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    if not binary.is_file():
        raise ValueError(f"binary is not a file: {binary}")
    root = (
        args.output_dir.expanduser().resolve()
        if args.output_dir is not None
        else Path(tempfile.mkdtemp(prefix="wenyi-theme-smoke-"))
    )
    root.mkdir(parents=True, exist_ok=True)
    cases = [_run_case(binary, root, kind) for kind in ("source_epub", "generated_txt")]
    payload = {"schema_version": 1, "output_dir": str(root), "cases": cases}
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
