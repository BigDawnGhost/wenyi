from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from importlib.metadata import version
from pathlib import Path
from typing import Any

import tinycss2


def _walk(nodes: Iterable[Any]) -> Iterable[Any]:
    for node in nodes:
        yield node
        for attribute in ("content", "arguments"):
            children = getattr(node, attribute, None)
            if children:
                yield from _walk(children)


def _declarations(rule: Any) -> list[Any]:
    declarations = tinycss2.parse_declaration_list(
        rule.content, skip_comments=True, skip_whitespace=True
    )
    assert all(item.type == "declaration" for item in declarations), declarations
    return declarations


def _css_resource_check() -> bool:
    css = Path(__file__).with_name("theme_runtime_probe.css").read_text(encoding="utf-8")
    rules = tinycss2.parse_stylesheet(css, skip_comments=True, skip_whitespace=True)
    assert [rule.type for rule in rules] == ["qualified-rule", "at-rule"]

    base_rule, media = rules
    assert tinycss2.serialize(base_rule.prelude).strip() == '[data-tn-role="body"]'
    base_declarations = _declarations(base_rule)
    assert [(item.name, tinycss2.serialize(item.value).strip()) for item in base_declarations] == [
        ("font-size", "1.15em")
    ]

    assert media.lower_at_keyword == "media"
    assert tinycss2.serialize(media.prelude).strip() == "(prefers-color-scheme: dark)"
    nested = tinycss2.parse_rule_list(media.content, skip_comments=True, skip_whitespace=True)
    assert len(nested) == 1 and nested[0].type == "qualified-rule", nested
    assert tinycss2.serialize(nested[0].prelude).strip() == '[data-tn-role="body"]'
    dark_declarations = _declarations(nested[0])
    assert [(item.name, tinycss2.serialize(item.value).strip()) for item in dark_declarations] == [
        ("color", "#ddd")
    ]

    parsed = [*rules, *base_declarations, *nested, *dark_declarations]
    assert all(node.type != "error" for node in _walk(parsed))
    assert all(
        node.type != "url" and not (node.type == "function" and node.lower_name == "url")
        for node in _walk(parsed)
    )
    return True


def main() -> None:
    if len(sys.argv) != 1:
        raise SystemExit("theme runtime probe takes no arguments")
    checks = {"css_resource": _css_resource_check()}
    assert all(checks.values()), checks
    print(json.dumps({"css_parser_version": version("tinycss2"), "checks": checks}))


if __name__ == "__main__":
    main()
