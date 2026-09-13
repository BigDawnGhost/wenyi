from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from importlib.metadata import version
from pathlib import Path
from typing import Any

import quickjs
import tinycss2

MEMORY_LIMIT = 64 * 1024 * 1024
STACK_LIMIT = 512 * 1024

CLASSIFIER = r"""
function classify(node, document) {
  const shortStandalone = node.isTextBlock &&
    !node.textTruncated && node.textLength <= 80 &&
    !node.context.inTable && !node.context.inNavigation;
  if (shortStandalone && /^(?:chapter|part)\s+(?:[0-9]+|[IVXLCDM]+)(?:\s*[:.\u2014-]\s*\S.*)?$/iu.test(node.text)) {
    return { role: "heading", level: 1 };
  }
  if (node.tag === "p") return { role: "body" };
  return null;
}
"""

SNAPSHOT = {
    "apiVersion": 1,
    "nodes": [
        {
            "id": 0,
            "parentId": None,
            "previousSiblingId": None,
            "nextSiblingId": 1,
            "tag": "p",
            "namespace": "http://www.w3.org/1999/xhtml",
            "classes": [],
            "epubTypes": [],
            "ariaRole": None,
            "ariaLevel": None,
            "language": "en",
            "text": "Chapter IV — Arrival",
            "textLength": 20,
            "textTruncated": False,
            "isTextBlock": True,
            "context": {
                "inTable": False,
                "inNavigation": False,
                "inQuote": False,
                "inList": False,
            },
        },
        {
            "id": 1,
            "parentId": None,
            "previousSiblingId": 0,
            "nextSiblingId": None,
            "tag": "p",
            "namespace": "http://www.w3.org/1999/xhtml",
            "classes": [],
            "epubTypes": [],
            "ariaRole": None,
            "ariaLevel": None,
            "language": "en",
            "text": "This prose discusses chapter IV without being a heading.",
            "textLength": 56,
            "textTruncated": False,
            "isTextBlock": True,
            "context": {
                "inTable": False,
                "inNavigation": False,
                "inQuote": False,
                "inList": False,
            },
        },
    ],
}


def _context(time_limit: int) -> quickjs.Context:
    context = quickjs.Context()
    context.set_memory_limit(MEMORY_LIMIT)
    context.set_max_stack_size(STACK_LIMIT)
    context.set_time_limit(time_limit)
    return context


def _classification_checks() -> tuple[bool, bool]:
    context = _context(1)
    context.eval(
        """
        const __host = (() => {
          "use strict";
          const parse = JSON.parse.bind(JSON);
          const stringify = JSON.stringify.bind(JSON);
          const deepFreeze = value => {
            if (value !== null && typeof value === "object" && !Object.isFrozen(value)) {
              Object.freeze(value);
              for (const child of Object.values(value)) deepFreeze(child);
            }
            return value;
          };
          if (!Reflect.deleteProperty(globalThis, "Date") ||
              !Reflect.deleteProperty(Math, "random") ||
              !Reflect.deleteProperty(globalThis, "Promise")) {
            throw new Error("failed to remove nondeterministic globals");
          }
          return Object.freeze({ parse, stringify, deepFreeze });
        })();
        """
    )
    context.eval(
        f"""
        const __classify = (() => {{
          "use strict";
          {CLASSIFIER}
          if (typeof classify !== "function") throw new TypeError("classify is required");
          return classify;
        }})();
        """
    )
    context.set_time_limit(2)
    snapshot_json = json.dumps(SNAPSHOT, ensure_ascii=True, separators=(",", ":"))
    result_json = context.eval(
        f"""
        (() => {{
          "use strict";
          const document = __host.deepFreeze(__host.parse({json.dumps(snapshot_json)}));
          const outputs = document.nodes.map(node => __classify(node, document));
          const forbidden = [
            "require", "importScripts", "load", "std", "os", "scriptArgs",
            "fetch", "XMLHttpRequest", "WebSocket", "process", "Deno", "Bun",
            "Python", "python", "pyodide", "setTimeout", "setInterval"
          ];
          const noHost = forbidden.every(name => typeof globalThis[name] === "undefined") &&
            typeof Date === "undefined" && typeof Math.random === "undefined" &&
            typeof Promise === "undefined";
          const frozen = Object.isFrozen(document) && Object.isFrozen(document.nodes) &&
            document.nodes.every(node => Object.isFrozen(node) &&
              Object.isFrozen(node.context) && Object.isFrozen(node.classes) &&
              Object.isFrozen(node.epubTypes));
          return __host.stringify({{ outputs, frozen, noHost }});
        }})()
        """
    )
    result = json.loads(result_json)
    expected = [{"role": "heading", "level": 1}, {"role": "body"}]
    assert result["outputs"] == expected, result["outputs"]
    assert result["frozen"] is True
    assert result["noHost"] is True
    return True, True


def _cpu_interrupt_check() -> bool:
    context = _context(1)
    try:
        context.eval("for (;;) {}")
    except quickjs.JSException as error:
        assert "InternalError: interrupted" in str(error), str(error)
        return True
    raise AssertionError("QuickJS CPU limit did not interrupt execution")


def _memory_limit_check() -> bool:
    context = _context(1)
    try:
        context.eval("new ArrayBuffer(128 * 1024 * 1024)")
    except quickjs.JSException as error:
        category = str(error).splitlines()[0]
        assert category in {"null", "InternalError: out of memory"}, str(error)
        return True
    raise AssertionError("QuickJS memory limit allowed a 128 MiB allocation")


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
    classification, no_host = _classification_checks()
    checks = {
        "classification": classification,
        "cpu_interrupt": _cpu_interrupt_check(),
        "memory_limit": _memory_limit_check(),
        "css_resource": _css_resource_check(),
        "no_host": no_host,
    }
    assert all(checks.values()), checks
    print(json.dumps({"engine_version": version("quickjs-ng"), "checks": checks}))


if __name__ == "__main__":
    main()
