from __future__ import annotations

import sys
import unittest
from unittest.mock import patch

from trans_novel.assemble.epub.rendering.theme.classification import (
    classify_resource,
    validate_script,
)
from trans_novel.assemble.epub.rendering.theme.contracts import (
    RoleAssignment,
    ThemeBundle,
    ThemeError,
)


def _bundle(script: str | None) -> ThemeBundle:
    return ThemeBundle(
        script.encode() if script is not None else None,
        None,
        None,
        "digest",
        "0.16.2.1" if script is not None else "",
        1,
        "epub-theme-v1",
        (),
    )


def _snapshot(count: int = 2) -> dict[str, object]:
    return {
        "apiVersion": 1,
        "nodes": [
            {
                "id": index,
                "parentId": None,
                "previousSiblingId": index - 1 if index else None,
                "nextSiblingId": index + 1 if index + 1 < count else None,
                "tag": "p",
                "namespace": "http://www.w3.org/1999/xhtml",
                "classes": [],
                "epubTypes": [],
                "ariaRole": None,
                "ariaLevel": None,
                "language": "zh",
                "text": str(index),
                "textLength": 1,
                "textTruncated": False,
                "isTextBlock": True,
                "context": {
                    "inTable": False,
                    "inNavigation": False,
                    "inQuote": False,
                    "inList": False,
                },
            }
            for index in range(count)
        ],
    }


class TestThemeClassification(unittest.TestCase):
    def test_valid_custom_roles_preserve_document_order_and_null_skips(self) -> None:
        assignments = classify_resource(
            _snapshot(),
            _bundle(
                "function classify(node) { return node.id === 0 ? null : {role: 'custom-role'};}"
            ),
        )
        self.assertEqual(assignments, (RoleAssignment(1, "custom-role"),))

    def test_large_result_crosses_native_string_boundary(self) -> None:
        assignments = classify_resource(
            _snapshot(2000),
            _bundle("function classify() { return {role: 'body'}; }"),
        )
        self.assertEqual(assignments, tuple(RoleAssignment(index, "body") for index in range(2000)))

    def test_absent_script_never_imports_engine(self) -> None:
        with patch.dict(sys.modules, {"quickjs": None}):
            self.assertEqual(classify_resource({"not": "a snapshot"}, _bundle(None)), ())
            validate_script(_bundle(None))

    def test_document_and_every_snapshot_container_are_frozen(self) -> None:
        script = """
        function classify(node, document) {
          const isolated = typeof Date === "undefined" &&
            typeof Math.random === "undefined" && typeof Promise === "undefined";
          const frozen = Object.isFrozen(document) && Object.isFrozen(document.nodes) &&
            Object.isFrozen(node) && Object.isFrozen(node.classes) &&
            Object.isFrozen(node.epubTypes) && Object.isFrozen(node.context);
          return frozen && isolated ? {role: "frozen"} : {role: "mutable"};
        }
        """
        self.assertEqual(
            classify_resource(_snapshot(1), _bundle(script)),
            (RoleAssignment(0, "frozen"),),
        )

    def test_each_resource_gets_fresh_script_state(self) -> None:
        bundle = _bundle(
            "let calls = 0; function classify() { calls += 1;"
            " return {role: calls === 1 ? 'first' : 'later'}; }"
        )
        expected = (RoleAssignment(0, "first"),)
        self.assertEqual(classify_resource(_snapshot(1), bundle), expected)
        self.assertEqual(classify_resource(_snapshot(1), bundle), expected)

    def test_invalid_return_shapes_fail_at_the_corresponding_node(self) -> None:
        scripts = {
            "undefined": "function classify() { return undefined; }",
            "array": "function classify() { return [{role: 'body'}]; }",
            "unknown": "function classify() { return {role: 'body', extra: true}; }",
            "bad role": "function classify() { return {role: 'Bad Role'}; }",
            "bad level": "function classify() { return {role: 'heading', level: 7}; }",
            "nonheading level": "function classify() { return {role: 'body', level: 1}; }",
            "undefined level": "function classify() { return {role: 'heading', level: undefined}; }",
            "getter": "function classify() { return {get role() { return 'body'; }}; }",
            "symbol": (
                "function classify() { const value = {role: 'body'};"
                " value[Symbol('x')] = 1; return value; }"
            ),
            "async": "async function classify() { return {role: 'body'}; }",
        }
        for label, script in scripts.items():
            with self.subTest(label=label), self.assertRaises(ThemeError) as failure:
                classify_resource(_snapshot(1), _bundle(script))
            self.assertEqual(failure.exception.code, "theme_script")
            self.assertEqual(str(failure.exception), "invalid_result")
            self.assertEqual(failure.exception.node_id, 0)

    def test_missing_syntax_and_exception_errors_are_sanitized(self) -> None:
        cases = (
            ("const helper = 1;", "missing_classify", None),
            ("function classify( {", "initialization_failed", None),
            ("throw null;", "initialization_failed", None),
            ("function classify() { throw null; }", "classification_failed", 0),
            (
                "function classify(node) { if (node.id === 1) throw new Error('secret payload'); return null; }",
                "classification_failed",
                1,
            ),
        )
        for script, detail, node_id in cases:
            with self.subTest(detail=detail), self.assertRaises(ThemeError) as failure:
                classify_resource(_snapshot(), _bundle(script))
            self.assertEqual(failure.exception.code, "theme_script")
            self.assertEqual(str(failure.exception), detail)
            self.assertEqual(failure.exception.node_id, node_id)
            self.assertNotIn("secret", str(failure.exception))

    def test_validate_script_executes_initialization_without_classifying(self) -> None:
        validate_script(_bundle("function classify() { throw new Error('not called'); }"))
        with self.assertRaises(ThemeError) as failure:
            validate_script(_bundle("const value = 1;"))
        self.assertEqual(
            (failure.exception.code, str(failure.exception)), ("theme_script", "missing_classify")
        )

    def test_cpu_and_memory_limits_are_named(self) -> None:
        with self.assertRaises(ThemeError) as cpu:
            classify_resource(
                _snapshot(1),
                _bundle("function classify() { for (;;) {} }"),
            )
        self.assertEqual((cpu.exception.code, str(cpu.exception)), ("theme_limit", "cpu_limit"))

        with self.assertRaises(ThemeError) as memory:
            classify_resource(
                _snapshot(1),
                _bundle("function classify() { return new ArrayBuffer(128 * 1024 * 1024); }"),
            )
        self.assertEqual(
            (memory.exception.code, str(memory.exception)),
            ("theme_limit", "memory_limit"),
        )

        with self.assertRaises(ThemeError) as stack:
            classify_resource(
                _snapshot(1),
                _bundle("function classify() { return classify(); }"),
            )
        self.assertEqual(
            (stack.exception.code, str(stack.exception)),
            ("theme_limit", "stack_limit"),
        )

    def test_snapshot_shape_and_limits_fail_before_execution(self) -> None:
        bundle = _bundle("function classify() { return null; }")
        for snapshot in (
            {"apiVersion": 1, "nodes": [{"id": 1}]},
            {"apiVersion": 1, "nodes": "not-an-array"},
        ):
            with self.subTest(snapshot=snapshot), self.assertRaises(ThemeError) as failure:
                classify_resource(snapshot, bundle)
            self.assertEqual(
                (failure.exception.code, str(failure.exception)),
                ("theme_script", "invalid_result"),
            )

        oversized = {"apiVersion": 1, "nodes": [{"id": 0, "text": "x" * (8 * 1024 * 1024)}]}
        with self.assertRaises(ThemeError) as failure:
            classify_resource(oversized, bundle)
        self.assertEqual(
            (failure.exception.code, str(failure.exception)),
            ("theme_limit", "snapshot_bytes"),
        )


if __name__ == "__main__":
    unittest.main()
