from __future__ import annotations

import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from trans_novel.assemble.epub.rendering.theme import (
    ThemeBundle,
    ThemeError,
    resolve_theme,
    semantic_output_digest,
)


class TestThemeLoading(unittest.TestCase):
    def test_builtin_bundle_is_an_immutable_css_snapshot(self) -> None:
        bundle = resolve_theme("builtin:chinese-reading", "builtin:bilingual")

        self.assertTrue(bundle.general_css)
        self.assertTrue(bundle.bilingual_css)
        self.assertEqual(bundle.policy_version, "epub-theme-v2")
        self.assertTrue(bundle.note_markers)
        with self.assertRaises(FrozenInstanceError):
            bundle.digest = "changed"  # type: ignore[misc]

    def test_custom_bytes_are_read_once_and_paths_do_not_affect_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            for base in (first, second):
                (base / "style.css").write_bytes(b"p { color: black; }")

            one = resolve_theme("style.css", None, config_base_dir=first)
            two = resolve_theme("style.css", None, config_base_dir=second)

            (first / "style.css").write_bytes(b"changed")
            self.assertEqual(one.general_css, b"p { color: black; }")
            self.assertEqual(one.digest, two.digest)
            self.assertFalse(one.note_markers)
            self.assertNotEqual(one.provenance, two.provenance)

    def test_custom_origin_is_metadata_not_resolution_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "style.css").write_bytes(b"")
            bundle = resolve_theme(
                "style.css",
                None,
                config_base_dir=root,
                origins={"override_theme.styles": "config.yaml"},
            )

            self.assertIn(("override_theme.styles.origin", "config.yaml"), bundle.provenance)

    def test_inactive_bundle_has_no_assets(self) -> None:
        bundle = resolve_theme(None, None)

        self.assertIsNone(bundle.general_css)
        self.assertIsNone(bundle.bilingual_css)

    def test_asset_failures_are_stable(self) -> None:
        with self.assertRaisesRegex(ThemeError, "^missing_base_dir$"):
            resolve_theme("relative.css", None)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ThemeError, "^asset_not_found$"):
                resolve_theme("missing.css", None, config_base_dir=root)

    def test_size_and_utf8_boundaries_fail_without_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, data, detail in (
                ("large.css", b"x" * (256 * 1024 + 1), "asset_too_large"),
                ("invalid.css", b"\xff", "asset_not_utf8"),
            ):
                with self.subTest(detail=detail):
                    (root / name).write_bytes(data)
                    with self.assertRaisesRegex(ThemeError, f"^{detail}$") as failure:
                        resolve_theme(name, None, config_base_dir=root)
                    self.assertEqual(failure.exception.resource, "override_theme.styles")

    def test_txt_digest_ignores_theme_and_layout(self) -> None:
        first = ThemeBundle(b"one", None, "first", "p", ())
        second = ThemeBundle(None, b"two", "second", "p", ())
        arguments = {
            "out_format": "txt",
            "mono": True,
            "bilingual": True,
            "bilingual_order": "target_first",
        }

        self.assertEqual(
            semantic_output_digest(first, layout_digest="a" * 64, **arguments),
            semantic_output_digest(second, layout_digest="b" * 64, **arguments),
        )
        self.assertEqual(
            semantic_output_digest(first, **arguments),
            semantic_output_digest(None, **arguments),
        )

    def test_epub_digest_tracks_css_and_layout_independently(self) -> None:
        first = ThemeBundle(b"one", None, "first", "p", ())
        second = ThemeBundle(b"changed", None, "second", "p", ())
        arguments = {
            "out_format": "epub",
            "mono": True,
            "bilingual": False,
            "bilingual_order": "target_first",
        }

        self.assertNotEqual(
            semantic_output_digest(first, layout_digest="a" * 64, **arguments),
            semantic_output_digest(second, layout_digest="a" * 64, **arguments),
        )
        self.assertNotEqual(
            semantic_output_digest(first, layout_digest="a" * 64, **arguments),
            semantic_output_digest(first, layout_digest="b" * 64, **arguments),
        )

    def test_bilingual_only_digest_does_not_depend_on_layout(self) -> None:
        bundle = ThemeBundle(None, b"source", "bilingual", "p", ())
        arguments = {
            "out_format": "epub",
            "mono": False,
            "bilingual": True,
            "bilingual_order": "source_first",
        }
        self.assertEqual(
            semantic_output_digest(bundle, layout_digest="a" * 64, **arguments),
            semantic_output_digest(bundle, layout_digest="b" * 64, **arguments),
        )

    def test_epub_digest_ignores_an_inactive_bundle(self) -> None:
        inactive = ThemeBundle(None, None, "unused", "p", ())
        arguments = {
            "out_format": "epub",
            "mono": True,
            "bilingual": False,
            "bilingual_order": "target_first",
        }
        self.assertEqual(
            semantic_output_digest(inactive, **arguments),
            semantic_output_digest(None, **arguments),
        )


if __name__ == "__main__":
    unittest.main()
