from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from trans_novel.assemble.epub.rendering.theme.contracts import ThemeError
from trans_novel.assemble.epub.rendering.theme.css import (
    CssRule,
    declaration_properties,
    parse_theme_css,
)


class TestThemeCss(unittest.TestCase):
    def test_empty_and_light_dark_rules_preserve_order(self) -> None:
        self.assertEqual(parse_theme_css(b"/* empty */"), ())
        rules = parse_theme_css(
            b"p { color: black; }"
            b"@media (prefers-color-scheme: dark) { p:first-child { color: white } }"
            b"@media ( prefers-color-scheme : light ) { p:not(.note) { color: navy } }"
        )
        self.assertEqual([rule.media for rule in rules], [None, "dark", "light"])
        self.assertEqual([rule.selector for rule in rules], ["p", "p:first-child", "p:not(.note)"])
        self.assertEqual([rule.declarations[0].value for rule in rules], ["black", "white", "navy"])
        with self.assertRaises(FrozenInstanceError):
            rules[0].selector = "div"  # type: ignore[misc]

    def test_values_are_tokenized_without_splitting_strings(self) -> None:
        rule = parse_theme_css(b'p { font-family: "A; B"; color: rgb(1, 2, 3) !important }')[0]
        self.assertEqual(rule.declarations[0].value, '"A; B"')
        self.assertTrue(rule.declarations[1].important)
        vertical = parse_theme_css(b"sup { vertical-align: super }")[0].declarations[0]
        self.assertEqual((vertical.name, vertical.value), ("vertical-align", "super"))

    def test_urls_vars_and_custom_properties_are_rejected_recursively(self) -> None:
        for css in (
            rb"p { background-color: u\72l(image.png) }",
            b"p { vertical-align: url(image.png) }",
            b"p { color: rgb(var(--tone), 0, 0) }",
            b"p[data-x='ok'] { --tone: red }",
            rb"p:n\6ft(u\72l(foo)) { color: red }",
        ):
            with (
                self.subTest(css=css),
                self.assertRaisesRegex(ThemeError, "^(forbidden_value|forbidden_property)$"),
            ):
                parse_theme_css(css)

    def test_dynamic_pseudo_elements_and_namespaces_are_rejected(self) -> None:
        for selector in ("a:hover", "p:not(:focus)", "p::before", "svg|a", "*|a", "|a"):
            with (
                self.subTest(selector=selector),
                self.assertRaisesRegex(ThemeError, "^unsupported_selector$"),
            ):
                parse_theme_css(f"{selector} {{ color: red }}".encode())

    def test_static_selector_lists_and_zero_match_selectors_are_valid(self) -> None:
        rules = parse_theme_css(
            b"article > p:nth-child(2n of .body), [lang|=zh]:empty { text-indent: 2em }"
        )
        self.assertEqual(len(rules), 1)

    def test_forbidden_rules_properties_and_invalid_syntax_fail_stably(self) -> None:
        cases = (
            (b"@import 'other.css';", "forbidden_rule"),
            (b"@font-face { font-family: x; }", "forbidden_rule"),
            (b"@media print { p { color: red } }", "forbidden_rule"),
            (
                b"@media (prefers-color-scheme: dark) { @media (prefers-color-scheme: light) {} }",
                "forbidden_rule",
            ),
            (b"p { display: block }", "forbidden_property"),
            (b"p { animation: none }", "forbidden_property"),
            (b"p { color: red; broken }", "invalid_css"),
            (b"p { background: red }", "forbidden_property"),
            (b"p { font-variant-caps: small-caps }", "forbidden_property"),
        )
        for css, detail in cases:
            with (
                self.subTest(detail=detail),
                self.assertRaisesRegex(ThemeError, f"^{detail}$") as error,
            ):
                parse_theme_css(css, resource="custom-theme")
            self.assertEqual(error.exception.code, "theme_css")
            self.assertEqual(error.exception.resource, "custom-theme")

    def test_deep_tokens_fail_with_a_stable_theme_error(self) -> None:
        nested = ("f(" * 1200 + "red" + ")" * 1200).encode()
        with self.assertRaisesRegex(ThemeError, "^invalid_css$") as error:
            parse_theme_css(b"p { color:" + nested + b" }", resource="deep-theme")
        self.assertEqual(error.exception.code, "theme_css")
        self.assertEqual(error.exception.resource, "deep-theme")

    def test_property_footprints_are_conservative_without_merging_longhands(self) -> None:
        self.assertFalse(
            declaration_properties("font-size") & declaration_properties("font-weight")
        )
        self.assertIn("line-height", declaration_properties("font"))
        self.assertTrue(
            declaration_properties("font-variant") & declaration_properties("font-variant-caps")
        )
        self.assertTrue(
            declaration_properties("font") & declaration_properties("font-variant-numeric")
        )
        self.assertTrue(
            declaration_properties("background") & declaration_properties("background-color")
        )
        self.assertTrue(
            declaration_properties("margin-inline-start") & declaration_properties("margin-left")
        )
        self.assertFalse(
            declaration_properties("margin-top") & declaration_properties("padding-top")
        )
        self.assertTrue(
            declaration_properties("border-start-start-radius")
            & declaration_properties("border-top-left-radius")
        )
        self.assertEqual(declaration_properties("unknown"), frozenset())
        self.assertIsInstance(CssRule("p", ()).declarations, tuple)
