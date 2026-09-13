"""与输入源绑定的输出选择持久化回归测试。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from trans_novel.pipeline.state import (
    RunStore,
    load_output_selection,
    save_output_selection,
)

_SOURCE_A = "a" * 64
_SOURCE_B = "b" * 64
_INVALID = "^theme_config: invalid_output_selection$"
_UNKNOWN = "^theme_config: unknown_output_selection_schema$"
_MISMATCH = "^theme_config: source_mismatch$"


def _selection_path(store: RunStore) -> Path:
    return Path(store.run_dir, "output_selection.json")


class TestOutputSelection(unittest.TestCase):
    def test_missing_then_durable_round_trip_is_source_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(os.path.join(directory, "book"))
            self.assertIsNone(load_output_selection(store, source_bytes_sha256=_SOURCE_A))

            selection = {
                "mono": True,
                "bilingual": {"enabled": False, "order": "source_first"},
                "override_theme": None,
                "weights": [1, 0.25],
            }
            origins = {"bilingual_styles": "config.example.yaml"}
            with store.lock():
                save_output_selection(
                    store,
                    selection,
                    origins,
                    source_bytes_sha256=_SOURCE_A,
                )

            reopened = RunStore(store.run_dir)
            saved = load_output_selection(reopened, source_bytes_sha256=_SOURCE_A)
            self.assertIsNotNone(saved)
            assert saved is not None
            self.assertEqual(saved.selection, selection)
            self.assertEqual(saved.origins, origins)
            self.assertEqual(saved.source_bytes_sha256, _SOURCE_A)
            self.assertFalse(os.path.exists(store.manifest_path))

            with self.assertRaisesRegex(ValueError, _MISMATCH):
                load_output_selection(reopened, source_bytes_sha256=_SOURCE_B)

    def test_load_classifies_untrusted_records_without_leaking_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(os.path.join(directory, "book"))
            path = _selection_path(store)

            with self.assertRaisesRegex(ValueError, _INVALID):
                load_output_selection(store, source_bytes_sha256="A" * 64)

            cases = (
                ([], _INVALID),
                ({"selection": {}, "origins": {}}, _INVALID),
                ({"schema_version": 2}, _INVALID),
                ({"schema_version": 3}, _UNKNOWN),
            )
            for payload, expected in cases:
                with self.subTest(payload=payload):
                    store.write_json(str(path), payload)
                    with self.assertRaisesRegex(ValueError, expected):
                        load_output_selection(store, source_bytes_sha256=_SOURCE_A)

            store.write_json(
                str(path),
                {
                    "schema_version": 1,
                    "source_bytes_sha256": _SOURCE_A,
                    "selection": {"not_finite": float("nan")},
                    "origins": {},
                },
            )
            with self.assertRaisesRegex(ValueError, _INVALID):
                load_output_selection(store, source_bytes_sha256=_SOURCE_A)

            path.write_text("{private book text", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, _INVALID) as raised:
                load_output_selection(store, source_bytes_sha256=_SOURCE_A)
            self.assertNotIn("private book text", str(raised.exception))

    def test_valid_v1_snapshot_is_discarded_only_after_source_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(os.path.join(directory, "book"))
            payload = {
                "schema_version": 1,
                "source_bytes_sha256": _SOURCE_A,
                "selection": {
                    "override_theme": {
                        "rules": "builtin:legacy-javascript",
                        "styles": "builtin:chinese-reading",
                    }
                },
                "origins": {"override_theme.rules": "legacy.yaml"},
            }
            store.write_json(str(_selection_path(store)), payload)

            with self.assertRaisesRegex(ValueError, _MISMATCH):
                load_output_selection(store, source_bytes_sha256=_SOURCE_B)
            self.assertIsNone(load_output_selection(store, source_bytes_sha256=_SOURCE_A))
            self.assertIn(
                '"event": "output_selection_obsolete"',
                Path(store.event_log_path).read_text(encoding="utf-8"),
            )

    def test_save_rejects_non_json_values_before_creating_a_record(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(os.path.join(directory, "book"))
            invalid_records = (
                ({"asset": object()}, {}),
                ({"number": float("inf")}, {}),
                ({"tuple": (1, 2)}, {}),
                ({1: "value"}, {}),
                ({}, {"slot": 1}),
            )
            for selection, origins in invalid_records:
                with self.subTest(selection=selection, origins=origins):
                    with store.lock(), self.assertRaisesRegex(ValueError, _INVALID):
                        save_output_selection(
                            store,
                            selection,
                            origins,
                            source_bytes_sha256=_SOURCE_A,
                        )
                    self.assertFalse(_selection_path(store).exists())

    def test_failed_save_preserves_selection_and_translation_state(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(os.path.join(directory, "book"))
            translation_path = Path(store.chapters_v2_dir, "ch0.json")
            store.write_json(str(translation_path), {"segments": [{"target": "既有译文"}]})
            translation_before = translation_path.read_bytes()

            with store.lock():
                save_output_selection(store, {"mono": True}, {}, source_bytes_sha256=_SOURCE_A)
            selection_path = _selection_path(store)
            selection_before = selection_path.read_bytes()

            with store.lock(), self.assertRaisesRegex(ValueError, _MISMATCH):
                save_output_selection(store, {"mono": False}, {}, source_bytes_sha256=_SOURCE_B)
            self.assertEqual(selection_path.read_bytes(), selection_before)
            self.assertEqual(translation_path.read_bytes(), translation_before)
            self.assertFalse(os.path.exists(store.manifest_path))

            selection_path.write_text("corrupt", encoding="utf-8")
            corrupt_before = selection_path.read_bytes()
            with store.lock(), self.assertRaisesRegex(ValueError, _INVALID):
                save_output_selection(store, {"mono": False}, {}, source_bytes_sha256=_SOURCE_A)
            self.assertEqual(selection_path.read_bytes(), corrupt_before)
            self.assertEqual(translation_path.read_bytes(), translation_before)


if __name__ == "__main__":
    unittest.main()
