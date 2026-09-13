from __future__ import annotations

import unittest

from trans_novel.assemble.epub.rendering.theme.cascade import (
    normalize_inline,
    source_specificity_bound,
)
from trans_novel.assemble.epub.rendering.theme.contracts import ThemeError
from trans_novel.assemble.epub.rendering.theme.css import CssDeclaration, CssRule


def _rule(name: str, media: str | None = None) -> CssRule:
    return CssRule("p", (CssDeclaration(name, "inherit"),), media)  # type: ignore[arg-type]


class TestInlineNormalization(unittest.TestCase):
    def test_font_shorthand_value_tokens_survive_priority_removal(self) -> None:
        style = 'font: italic 1em/1.2 "A;B" !important; color: red /* keep */;'
        normalized, names = normalize_inline(style, [_rule("font-family")])
        self.assertEqual(
            normalized,
            'font: italic 1em/1.2 "A;B" ; color: red /* keep */;',
        )
        self.assertEqual(names, ("font",))

    def test_source_only_shorthands_and_longhands_conflict_with_theme_properties(self) -> None:
        background = "background: url(a.png) left/cover no-repeat black !important"
        normalized, names = normalize_inline(background, [_rule("background-color")])
        self.assertIn("url(a.png) left/cover no-repeat black", normalized)
        self.assertNotIn("important", normalized)
        self.assertEqual(names, ("background",))

        variant, names = normalize_inline(
            "font-variant-caps: small-caps !important",
            [_rule("font-variant")],
        )
        self.assertNotIn("important", variant)
        self.assertEqual(names, ("font-variant-caps",))

    def test_source_background_shorthand_requires_complete_color_media_coverage(self) -> None:
        with self.assertRaisesRegex(ThemeError, "^media_coverage_required$"):
            normalize_inline("background: red !important", [_rule("background-color", "dark")])

    def test_unrelated_or_nonimportant_declarations_are_byte_for_byte_unchanged(self) -> None:
        style = "display:block!important;color:red; --source: 1"
        self.assertEqual(normalize_inline(style, [_rule("font-size")]), (style, ()))
        self.assertEqual(normalize_inline("all: initial", [_rule("color")]), ("all: initial", ()))

    def test_unconditional_or_complete_media_coverage_allows_demotion(self) -> None:
        style = "font: serif !important"
        for rules in (
            [_rule("font-family")],
            [_rule("font-family", "light"), _rule("font-family", "dark")],
        ):
            with self.subTest(rules=rules):
                normalized, names = normalize_inline(style, rules)
                self.assertNotIn("important", normalized)
                self.assertEqual(names, ("font",))

    def test_partial_media_coverage_fails_without_mutating_other_declarations(self) -> None:
        with self.assertRaisesRegex(ThemeError, "^media_coverage_required$") as error:
            normalize_inline(
                "font: serif !important; color: red !important",
                [_rule("font-family", "dark"), _rule("color")],
                resource="chapter",
                node_id=7,
            )
        self.assertEqual(error.exception.code, "theme_css")
        self.assertEqual(error.exception.resource, "chapter")
        self.assertEqual(error.exception.node_id, 7)

    def test_duplicate_normalized_names_preserve_source_order(self) -> None:
        normalized, names = normalize_inline(
            "color:red!important;color:blue!important",
            [_rule("color")],
        )
        self.assertNotIn("important", normalized)
        self.assertEqual(names, ("color", "color"))

    def test_important_all_fails_only_when_theme_declarations_match(self) -> None:
        with self.assertRaisesRegex(ThemeError, "^unsupported_source_inline_shorthand$"):
            normalize_inline("all: initial !important", [_rule("color")])
        style = "all: initial !important"
        self.assertEqual(normalize_inline(style, [CssRule("p", ())]), (style, ()))

    def test_active_inline_animation_fails_only_for_matching_theme_declarations(self) -> None:
        style = "-webkit-animation-name: fade; transition: none"
        with self.assertRaisesRegex(ThemeError, "^source_animation$") as error:
            normalize_inline(style, [_rule("color")], resource="chapter", node_id=9)
        self.assertEqual(error.exception.node_id, 9)
        self.assertEqual(normalize_inline(style, [CssRule("p", ())]), (style, ()))

    def test_malformed_inline_css_always_fails(self) -> None:
        with self.assertRaisesRegex(ThemeError, "^invalid_inline_css$"):
            normalize_inline("color: red; broken", [])

    def test_deep_inline_tokens_fail_with_a_stable_theme_error(self) -> None:
        style = "color:" + "f(" * 1200 + "red" + ")" * 1200
        with self.assertRaisesRegex(ThemeError, "^invalid_inline_css$") as error:
            normalize_inline(style, [], resource="chapter", node_id=11)
        self.assertEqual(error.exception.code, "theme_css")
        self.assertEqual(error.exception.resource, "chapter")
        self.assertEqual(error.exception.node_id, 11)


class TestSourceSpecificityBound(unittest.TestCase):
    def test_counts_every_identifier_hash_recursively_and_adds_one(self) -> None:
        sheets = {
            "one": b"#a, p:not(#b) { color: red }",
            "two": b"@media screen { @supports (display: block) { article:has(#c) {} } }",
            "metadata": b"@font-face { font-family: X; src: local(X) } @keyframes x { from {} to {} }",
        }
        self.assertEqual(source_specificity_bound(sheets), 4)
        self.assertEqual(source_specificity_bound({}), 1)

    def test_specificity_limit_fails_instead_of_clamping(self) -> None:
        self.assertEqual(source_specificity_bound({"x": (b"#x" * 127) + b"{}"}), 128)
        with self.assertRaisesRegex(ThemeError, "^specificity_limit$"):
            source_specificity_bound({"x": (b"#x" * 128) + b"{}"})

    def test_layers_nesting_imports_and_unsupported_rules_fail_explicitly(self) -> None:
        cases = (
            (b"@layer base { p {} }", "source_layers"),
            (b"p { & span { color: red } }", "source_nesting"),
            (b"@import 'x.css';", "import_not_resolved"),
            (b"@scope (.x) { p {} }", "unsupported_source_rule"),
            (b"p::before { color: red }", "unsupported_source_selector"),
            (b"@namespace svg url(x); svg|a {}", "unsupported_source_selector"),
        )
        for css, detail in cases:
            with self.subTest(detail=detail), self.assertRaisesRegex(ThemeError, f"^{detail}$"):
                source_specificity_bound({"source": css})

    def test_namespace_metadata_and_attribute_dash_match_are_supported(self) -> None:
        css = b"@namespace url(http://www.w3.org/1999/xhtml); [lang|=zh] {}"
        self.assertEqual(source_specificity_bound({"source": css}), 1)

    def test_page_declarations_are_valid_and_do_not_add_selector_ids(self) -> None:
        css = b"@page { margin-bottom: 5pt; margin-top: 5pt } #chapter {}"
        self.assertEqual(source_specificity_bound({"source": css}), 2)
        with self.assertRaisesRegex(ThemeError, "^source_nesting$"):
            source_specificity_bound({"source": b"@page { @top-left { content: 'chapter' } }"})

    def test_active_animation_and_transition_fail_but_exact_none_is_allowed(self) -> None:
        for declaration in (
            b"animation: fade 1s",
            b"transition-property: color",
            b"-webkit-animation-name: fade",
        ):
            with (
                self.subTest(declaration=declaration),
                self.assertRaisesRegex(ThemeError, "^source_animation$"),
            ):
                source_specificity_bound({"source": b"p{" + declaration + b"}"})
        self.assertEqual(
            source_specificity_bound(
                {"source": b"p { animation: none; transition: /* ok */ none }"}
            ),
            1,
        )

    def test_malformed_source_css_is_rejected(self) -> None:
        with self.assertRaisesRegex(ThemeError, "^invalid_source_css$"):
            source_specificity_bound({"source": b"p { color: red; broken }"})
