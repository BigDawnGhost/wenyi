from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import yaml

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


def _config(case_dir: Path, *, source_lang: str) -> tuple[Config, dict[str, Any]]:
    raw = {
        "llm": fake_llm_dict(),
        "output": {
            "mono": True,
            "bilingual": {"enabled": True, "order": "target_first"},
            "override_theme": {
                "rules": "builtin:general",
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


def _assert_receipts(
    report: dict[str, Any], outputs: list[Path], source_sha256: str | None
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
        if not isinstance(role_counts, dict) or role_counts.get("body", 0) <= 0:
            raise AssertionError(f"missing body theme coverage: {output.name}")
        if not isinstance(resources, int) or resources <= 0:
            raise AssertionError(f"missing themed resources: {output.name}")
        evidence.append(
            {
                "name": output.name,
                "sha256": output_sha256,
                "body_roles": role_counts["body"],
                "resources": resources,
            }
        )
    return digest, evidence


def _run_case(binary: Path, root: Path, kind: str) -> dict[str, Any]:
    case_dir = root / kind
    case_dir.mkdir()
    source = case_dir / ("source.epub" if kind == "source_epub" else "source.txt")
    if kind == "source_epub":
        write_nested_toc_epub(str(source), toc_kind="nav")
    else:
        write_sample_txt(str(source))
    source_before = _sha256(source)

    config, strict_yaml = _config(case_dir, source_lang="en" if kind == "source_epub" else "ja")
    result = Application(config, client=FakeClient(handler=routing_handler)).run_all(
        str(source), out_format="txt"
    )
    store = result["store"]
    run_dir = Path(store.run_dir)
    targets_before = _targets(run_dir)
    usage_path = Path(store.usage_path)
    if not usage_path.is_file():
        raise AssertionError("completed run has no usage.json")
    usage_before = usage_path.read_bytes()

    config_path = (case_dir / "config.yaml").resolve()
    config_path.write_text(
        yaml.safe_dump(strict_yaml, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    mono = (case_dir / "output.epub").resolve()
    bilingual = (case_dir / "output-bi.epub").resolve()
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
            str(mono),
        ],
        check=True,
        timeout=120,
        cwd=case_dir,
        env={**os.environ, "PATH": ""},
    )

    source_after = _sha256(source)
    targets_after = _targets(run_dir)
    usage_after = usage_path.read_bytes()
    if source_after != source_before:
        raise AssertionError("assembly changed the source")
    if targets_after != targets_before:
        raise AssertionError("assembly changed persisted targets")
    if usage_after != usage_before:
        raise AssertionError("assembly changed usage.json")

    report = store.load_epub_verification()
    if not isinstance(report, dict):
        raise AssertionError("assembly did not persist a publication report")
    expected_source = source_before if kind == "source_epub" else None
    digest, outputs = _assert_receipts(report, [mono, bilingual], expected_source)
    structural_pass = None
    if kind == "source_epub":
        triplet = validate_epub_triplet(
            source,
            mono,
            bilingual,
            publication_report=report,
            output_digest=digest,
        )
        structural_pass = triplet.get("structural_pass")
        if structural_pass is not True:
            raise AssertionError(f"source EPUB triplet failed: {triplet}")
    elif report.get("triplet") is not None:
        raise AssertionError("generated EPUB report must not contain a source triplet")

    return {
        "kind": kind,
        "source_sha256": source_before,
        "output_digest": digest,
        "outputs": outputs,
        "source_unchanged": True,
        "targets_unchanged": True,
        "usage_unchanged": True,
        "triplet_structural_pass": structural_pass,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True, type=Path)
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    if not binary.is_file():
        raise ValueError(f"binary is not a file: {binary}")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        cases = [_run_case(binary, root, kind) for kind in ("source_epub", "generated_txt")]
    payload = {"schema_version": 1, "cases": cases}
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
