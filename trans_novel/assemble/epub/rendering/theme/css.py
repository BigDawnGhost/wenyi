from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal

import soupsieve
import tinycss2

from trans_novel.assemble.epub.rendering.theme.contracts import ThemeError

_STATIC_PSEUDOS = {
    "root",
    "scope",
    "empty",
    "first-child",
    "last-child",
    "only-child",
    "nth-child",
    "nth-last-child",
    "first-of-type",
    "last-of-type",
    "only-of-type",
    "nth-of-type",
    "nth-last-of-type",
    "not",
    "is",
    "where",
    "has",
    "lang",
    "dir",
}
_SELECTOR_ARGUMENT_PSEUDOS = {"not", "is", "where", "has"}
_NTH_PSEUDOS = {"nth-child", "nth-last-child", "nth-of-type", "nth-last-of-type"}


@dataclass(frozen=True, slots=True)
class CssDeclaration:
    name: str
    value: str
    important: bool = False


@dataclass(frozen=True, slots=True)
class CssRule:
    selector: str
    declarations: tuple[CssDeclaration, ...]
    media: Literal["light", "dark"] | None = None


def _box_footprints(result: dict[str, frozenset[str]]) -> None:
    sides = ("top", "right", "bottom", "left")
    logical_sides = {
        "block": sides,
        "inline": sides,
        "block-start": ("top", "right", "left"),
        "block-end": ("right", "bottom", "left"),
        "inline-start": sides,
        "inline-end": sides,
    }
    for family_name in ("margin", "padding"):
        all_sides = frozenset(f"{family_name}-{side}" for side in sides)
        result[family_name] = all_sides
        for side in sides:
            result[f"{family_name}-{side}"] = frozenset((f"{family_name}-{side}",))
        for suffix, possible_sides in logical_sides.items():
            result[f"{family_name}-{suffix}"] = frozenset(
                f"{family_name}-{side}" for side in possible_sides
            )
    border_atoms = tuple(
        f"border-{side}-{part}" for side in sides for part in ("width", "style", "color")
    )
    result["border"] = frozenset(border_atoms)
    for part in ("width", "style", "color"):
        atoms = frozenset(f"border-{side}-{part}" for side in sides)
        result[f"border-{part}"] = atoms
        for side in sides:
            result[f"border-{side}-{part}"] = frozenset((f"border-{side}-{part}",))
    for side in sides:
        result[f"border-{side}"] = frozenset(
            f"border-{side}-{part}" for part in ("width", "style", "color")
        )
    for logical, possible_sides in logical_sides.items():
        result[f"border-{logical}"] = frozenset(
            f"border-{side}-{part}"
            for side in possible_sides
            for part in ("width", "style", "color")
        )
        for part in ("width", "style", "color"):
            result[f"border-{logical}-{part}"] = frozenset(
                f"border-{side}-{part}" for side in possible_sides
            )
    corners = ("top-left", "top-right", "bottom-right", "bottom-left")
    radii = frozenset(f"border-{corner}-radius" for corner in corners)
    result["border-radius"] = radii
    for corner in corners:
        result[f"border-{corner}-radius"] = frozenset((f"border-{corner}-radius",))
    for corner in ("start-start", "start-end", "end-start", "end-end"):
        result[f"border-{corner}-radius"] = radii


def _property_footprints() -> dict[str, frozenset[str]]:
    result: dict[str, frozenset[str]] = {}

    def family(shorthand: str, members: tuple[str, ...]) -> None:
        result[shorthand] = frozenset(members)
        result.update((member, frozenset((member,))) for member in members)

    font_variants = (
        "font-variant-caps",
        "font-variant-numeric",
        "font-variant-ligatures",
        "font-variant-alternates",
        "font-variant-east-asian",
        "font-variant-position",
    )
    family(
        "font",
        (
            "font-family",
            "font-size",
            "font-style",
            "font-weight",
            "font-stretch",
            "line-height",
            *font_variants,
        ),
    )
    result["font-variant"] = frozenset(font_variants)
    family(
        "text-decoration",
        (
            "text-decoration-line",
            "text-decoration-style",
            "text-decoration-color",
            "text-decoration-thickness",
        ),
    )
    for name in (
        "letter-spacing",
        "word-spacing",
        "text-align",
        "text-indent",
        "text-transform",
        "color",
        "background-color",
        "box-shadow",
        "display",
        "height",
        "min-width",
        "width",
        "word-break",
        "overflow-wrap",
        "vertical-align",
    ):
        result[name] = frozenset((name,))
    background = (
        "background-color",
        "background-image",
        "background-position",
        "background-size",
        "background-repeat",
        "background-attachment",
        "background-origin",
        "background-clip",
    )
    result["background"] = frozenset(background)
    result.update((name, frozenset((name,))) for name in background)
    result["-webkit-text-fill-color"] = result["color"]
    result["hyphens"] = result["-webkit-hyphens"] = frozenset(("hyphens",))
    for position in ("before", "after", "inside"):
        footprint = frozenset((f"break-{position}",))
        result[f"break-{position}"] = result[f"page-break-{position}"] = footprint

    _box_footprints(result)
    return result


_PROPERTY_FOOTPRINTS = _property_footprints()
_SOURCE_ONLY_PROPERTIES = frozenset(
    {
        "background",
        "background-image",
        "background-position",
        "background-size",
        "background-repeat",
        "background-attachment",
        "background-origin",
        "background-clip",
        "font-variant-caps",
        "font-variant-numeric",
        "font-variant-ligatures",
        "font-variant-alternates",
        "font-variant-east-asian",
        "font-variant-position",
    }
)
_NOTE_PRESENTATION_PROPERTIES = frozenset({"display", "height", "min-width", "width"})
_THEME_PROPERTIES = (
    _PROPERTY_FOOTPRINTS.keys() - _SOURCE_ONLY_PROPERTIES - _NOTE_PRESENTATION_PROPERTIES
)


def declaration_properties(name: str) -> frozenset[str]:
    """返回声明影响的保守属性集合；未知属性不参与主题冲突。"""
    return _PROPERTY_FOOTPRINTS.get(name.lower(), frozenset())


def _fail(detail: str, resource: str) -> ThemeError:
    return ThemeError("theme_css", detail, resource=resource)


def _nested_tokens(token: object) -> list[object] | None:
    return getattr(token, "arguments", getattr(token, "content", None))


def _check_safe_tokens(
    tokens: list[object],
    resource: str,
    invalid_detail: str = "invalid_css",
) -> None:
    for token in tokens:
        if token.type == "error":
            raise _fail(invalid_detail, resource)
        if token.type == "url" or (token.type == "function" and token.lower_name in {"url", "var"}):
            raise _fail("forbidden_value", resource)
        nested = _nested_tokens(token)
        if nested is not None:
            _check_safe_tokens(nested, resource, invalid_detail)


def _check_namespaces(tokens: list[object], resource: str) -> None:
    for token in tokens:
        if token.type == "literal" and token.value == "|":
            raise _fail("unsupported_selector", resource)
        nested = _nested_tokens(token)
        if nested is not None:
            _check_namespaces(nested, resource)


def _check_selector_pseudos(tokens: list[object], resource: str) -> None:
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.type == "[] block":
            index += 1
            continue
        if token.type != "literal" or token.value != ":":
            index += 1
            continue
        if index + 1 >= len(tokens):
            raise _fail("invalid_selector", resource)
        pseudo = tokens[index + 1]
        if pseudo.type == "literal" and pseudo.value == ":":
            raise _fail("unsupported_selector", resource)
        if pseudo.type not in {"ident", "function"}:
            raise _fail("invalid_selector", resource)
        name = pseudo.lower_value if pseudo.type == "ident" else pseudo.lower_name
        if name not in _STATIC_PSEUDOS:
            raise _fail("unsupported_selector", resource)
        if pseudo.type == "function":
            if name in _SELECTOR_ARGUMENT_PSEUDOS:
                _check_selector_pseudos(pseudo.arguments, resource)
            elif name in _NTH_PSEUDOS:
                for offset, argument in enumerate(pseudo.arguments):
                    if argument.type == "ident" and argument.lower_value == "of":
                        _check_selector_pseudos(pseudo.arguments[offset + 1 :], resource)
                        break
        index += 2


def _validate_selector(tokens: list[object], resource: str) -> str:
    _check_safe_tokens(tokens, resource, "invalid_selector")
    _check_namespaces(tokens, resource)
    _check_selector_pseudos(tokens, resource)
    selector = tinycss2.serialize(tokens).strip()
    if not selector:
        raise _fail("invalid_selector", resource)
    try:
        soupsieve.compile(selector)
    except (soupsieve.SelectorSyntaxError, NotImplementedError, ValueError):
        raise _fail("invalid_selector", resource) from None
    return selector


def _parse_declarations(
    tokens: list[object],
    resource: str,
    properties: Collection[str],
) -> tuple[CssDeclaration, ...]:
    parsed = tinycss2.parse_declaration_list(tokens, skip_whitespace=True, skip_comments=True)
    declarations: list[CssDeclaration] = []
    for item in parsed:
        if item.type == "error":
            raise _fail("invalid_css", resource)
        if item.type != "declaration":
            raise _fail("forbidden_rule", resource)
        if item.name.startswith("--") or item.lower_name not in properties:
            raise _fail("forbidden_property", resource)
        _check_safe_tokens(item.value, resource)
        declarations.append(
            CssDeclaration(item.lower_name, tinycss2.serialize(item.value).strip(), item.important)
        )
    return tuple(declarations)


def _media_kind(tokens: list[object], resource: str) -> Literal["light", "dark"]:
    _check_safe_tokens(tokens, resource)
    significant = [token for token in tokens if token.type not in {"whitespace", "comment"}]
    if len(significant) != 1 or significant[0].type != "() block":
        raise _fail("forbidden_rule", resource)
    condition = [
        token for token in significant[0].content if token.type not in {"whitespace", "comment"}
    ]
    if (
        len(condition) != 3
        or condition[0].type != "ident"
        or condition[0].lower_value != "prefers-color-scheme"
        or condition[1].type != "literal"
        or condition[1].value != ":"
        or condition[2].type != "ident"
        or condition[2].lower_value not in {"light", "dark"}
    ):
        raise _fail("forbidden_rule", resource)
    return condition[2].lower_value


def _parse_rules(
    items: list[object],
    resource: str,
    media: Literal["light", "dark"] | None,
    properties: Collection[str],
) -> list[CssRule]:
    rules: list[CssRule] = []
    for item in items:
        if item.type == "error":
            raise _fail("invalid_css", resource)
        _check_safe_tokens(
            item.prelude,
            resource,
            "invalid_selector" if item.type == "qualified-rule" else "invalid_css",
        )
        if item.content is not None:
            _check_safe_tokens(item.content, resource)
        if item.type == "qualified-rule":
            rules.append(
                CssRule(
                    _validate_selector(item.prelude, resource),
                    _parse_declarations(item.content, resource, properties),
                    media,
                )
            )
            continue
        if item.type != "at-rule" or item.lower_at_keyword != "media" or media is not None:
            raise _fail("forbidden_rule", resource)
        if item.content is None:
            raise _fail("forbidden_rule", resource)
        nested = tinycss2.parse_rule_list(item.content, skip_whitespace=True, skip_comments=True)
        rules.extend(
            _parse_rules(
                nested,
                resource,
                _media_kind(item.prelude, resource),
                properties,
            )
        )
    return rules


def parse_theme_css(
    data: bytes,
    *,
    resource: str = "theme",
    note_presentation: bool = False,
) -> tuple[CssRule, ...]:
    """解析受限且不加载外部资源的主题 CSS。"""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise _fail("invalid_css", resource) from None
    try:
        items = tinycss2.parse_stylesheet(text, skip_whitespace=True, skip_comments=True)
        properties = (
            _THEME_PROPERTIES | _NOTE_PRESENTATION_PROPERTIES
            if note_presentation
            else _THEME_PROPERTIES
        )
        return tuple(_parse_rules(items, resource, None, properties))
    except RecursionError:
        raise _fail("invalid_css", resource) from None
