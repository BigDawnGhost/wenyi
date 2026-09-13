from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from trans_novel.assemble.epub.rendering.theme import (
    ThemeBundle,
    ThemeError,
    resolve_theme,
    semantic_output_digest,
)

_VERSION = "0.16.2.1"


class TestThemeLoading(unittest.TestCase):
    def test_builtin_bundle_is_an_immutable_byte_snapshot(self) -> None:
        with patch(
            "trans_novel.assemble.epub.rendering.theme.loading.metadata.version",
            return_value=_VERSION,
        ):
            bundle = resolve_theme(
                "builtin:general",
                "builtin:chinese-reading",
                "builtin:bilingual",
            )

        self.assertIn(b"function classify(node, document)", bundle.script or b"")
        self.assertTrue(bundle.general_css)
        self.assertTrue(bundle.bilingual_css)
        self.assertEqual(bundle.engine_version, _VERSION)
        self.assertEqual(bundle.api_version, 1)
        self.assertEqual(bundle.policy_version, "epub-theme-v1")
        with self.assertRaises(FrozenInstanceError):
            bundle.digest = "changed"  # type: ignore[misc]

    def test_packaged_classifier_applies_general_priorities(self) -> None:
        import quickjs

        with patch(
            "trans_novel.assemble.epub.rendering.theme.loading.metadata.version",
            return_value=_VERSION,
        ):
            script = resolve_theme("builtin:general", "builtin:chinese-reading", None).script
        base = {
            "epubTypes": [],
            "ariaRole": None,
            "ariaLevel": None,
            "textTruncated": False,
            "isTextBlock": True,
            "context": dict.fromkeys(("inTable", "inList", "inNavigation", "inQuote"), False),
        }
        nodes = [
            {**base, "tag": "h1", "text": "第一章", "textLength": 3, "epubTypes": ["subtitle"]},
            {**base, "tag": "h2", "text": "Chapter IV", "textLength": 10},
            {**base, "tag": "p", "text": "Chapter IV — Arrival", "textLength": 20},
            {**base, "tag": "p", "text": "This discusses Chapter IV.", "textLength": 25},
        ]
        context = quickjs.Context()
        context.eval((script or b"").decode())
        result = json.loads(
            context.eval(f"JSON.stringify({json.dumps(nodes)}.map(n => classify(n, {{}})))")
        )
        self.assertEqual(
            result,
            [
                {"role": "subtitle"},
                {"role": "chapter-number"},
                {"role": "heading", "level": 1},
                {"role": "body"},
            ],
        )

    def test_custom_bytes_are_read_once_and_paths_do_not_affect_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            for base in (first, second):
                (base / "rules.js").write_bytes(b"function classify(){return null}")
                (base / "style.css").write_bytes(b"p { color: black; }")

            with patch(
                "trans_novel.assemble.epub.rendering.theme.loading.metadata.version",
                return_value=_VERSION,
            ):
                one = resolve_theme("rules.js", "style.css", None, config_base_dir=first)
                two = resolve_theme("rules.js", "style.css", None, config_base_dir=second)

            (first / "rules.js").write_bytes(b"changed")
            self.assertEqual(one.script, b"function classify(){return null}")
            self.assertEqual(one.digest, two.digest)
            self.assertNotEqual(one.provenance, two.provenance)

    def test_custom_origin_is_metadata_not_resolution_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "rules.js").write_bytes(b"")
            (root / "style.css").write_bytes(b"")
            with patch(
                "trans_novel.assemble.epub.rendering.theme.loading.metadata.version",
                return_value=_VERSION,
            ):
                bundle = resolve_theme(
                    "rules.js",
                    "style.css",
                    None,
                    config_base_dir=root,
                    origins={"override_theme.rules": "config.yaml"},
                )

            self.assertIn(("override_theme.rules.origin", "config.yaml"), bundle.provenance)
            self.assertNotIn(("override_theme.styles.origin", "config.yaml"), bundle.provenance)

    def test_inactive_bundle_does_not_query_engine_metadata(self) -> None:
        with patch("trans_novel.assemble.epub.rendering.theme.loading.metadata.version") as version:
            bundle = resolve_theme(None, None, None)

        version.assert_not_called()
        self.assertEqual(bundle.engine_version, "")
        self.assertIsNone(bundle.script)
        self.assertIsNone(bundle.general_css)
        self.assertIsNone(bundle.bilingual_css)

    def test_pair_and_asset_failures_are_stable(self) -> None:
        with self.assertRaisesRegex(ThemeError, "^theme_pair_required$") as pair:
            resolve_theme("builtin:general", None, None)
        self.assertEqual(pair.exception.code, "theme_config")

        with self.assertRaisesRegex(ThemeError, "^missing_base_dir$"):
            resolve_theme("relative.js", "relative.css", None)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "style.css").write_bytes(b"")
            with self.assertRaisesRegex(ThemeError, "^asset_not_found$"):
                resolve_theme("missing.js", "style.css", None, config_base_dir=root)

    def test_size_and_utf8_boundaries_fail_without_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            style = root / "style.css"
            style.write_bytes(b"")
            for name, data, detail in (
                ("large.js", b"x" * (256 * 1024 + 1), "asset_too_large"),
                ("invalid.js", b"\xff", "asset_not_utf8"),
            ):
                with self.subTest(detail=detail):
                    (root / name).write_bytes(data)
                    with self.assertRaisesRegex(ThemeError, f"^{detail}$") as failure:
                        resolve_theme(name, style.name, None, config_base_dir=root)
                    self.assertEqual(failure.exception.resource, "override_theme.rules")

    def test_engine_metadata_is_required_only_for_active_assets(self) -> None:
        from importlib.metadata import PackageNotFoundError

        with (
            patch(
                "trans_novel.assemble.epub.rendering.theme.loading.metadata.version",
                side_effect=PackageNotFoundError("quickjs-ng"),
            ),
            self.assertRaisesRegex(ThemeError, "^engine_not_installed$"),
        ):
            resolve_theme(None, None, "builtin:bilingual")

    def test_txt_digest_ignores_bundle_bytes_and_engine(self) -> None:
        first = ThemeBundle(b"one", b"two", None, "first", "engine-a", 1, "p", ())
        second = ThemeBundle(b"changed", None, b"", "second", "engine-b", 1, "p", ())
        arguments = {
            "out_format": "txt",
            "mono": True,
            "bilingual": True,
            "bilingual_order": "target_first",
        }

        self.assertEqual(
            semantic_output_digest(first, **arguments),  # type: ignore[arg-type]
            semantic_output_digest(second, **arguments),  # type: ignore[arg-type]
        )
        self.assertEqual(
            semantic_output_digest(first, **arguments),  # type: ignore[arg-type]
            semantic_output_digest(None, **arguments),  # type: ignore[arg-type]
        )
        self.assertNotEqual(
            semantic_output_digest(
                first,
                out_format="epub",
                mono=True,
                bilingual=True,
                bilingual_order="target_first",
            ),
            semantic_output_digest(
                second,
                out_format="epub",
                mono=True,
                bilingual=True,
                bilingual_order="target_first",
            ),
        )

    def test_epub_digest_ignores_an_inactive_bundle(self) -> None:
        inactive = ThemeBundle(None, None, None, "unused", "", 1, "p", ())
        arguments = {
            "out_format": "epub",
            "mono": True,
            "bilingual": False,
            "bilingual_order": "target_first",
        }
        self.assertEqual(
            semantic_output_digest(inactive, **arguments),  # type: ignore[arg-type]
            semantic_output_digest(None, **arguments),  # type: ignore[arg-type]
        )
