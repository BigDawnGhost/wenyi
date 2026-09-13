from __future__ import annotations

from collections.abc import Mapping, Sequence

import soupsieve
import tinycss2

from trans_novel.assemble.epub.rendering.theme.contracts import ThemeError
from trans_novel.assemble.epub.rendering.theme.css import CssRule, declaration_properties

_MAX_SPECIFICITY = 128
_MAX_CONDITIONAL_DEPTH = 64
_CONDITIONAL_RULES = {"media", "supports"}
_METADATA_RULES = {"charset", "namespace"}


def _fail(
    detail: str,
    resource: str,
    *,
    node_id: int | None = None,
) -> ThemeError:
    return ThemeError("theme_css", detail, resource=resource, node_id=node_id)


def _source_fail(detail: str, resource: str) -> ThemeError:
    return ThemeError("theme_css", detail, resource=resource)


def _coverage(rules: Sequence[CssRule]) -> dict[str, set[str | None]]:
    coverage: dict[str, set[str | None]] = {}
    for rule in rules:
        for declaration in rule.declarations:
            for prop in declaration_properties(declaration.name):
                coverage.setdefault(prop, set()).add(rule.media)
    return coverage


def normalize_inline(
    style: str,
    rules: Sequence[CssRule],
    *,
    resource: str = "theme",
    node_id: int | None = None,
) -> tuple[str, tuple[str, ...]]:
    """仅降低已被完整主题覆盖的源内联声明优先级。"""
    try:
        parsed = tinycss2.parse_declaration_list(
            style,
            skip_whitespace=False,
            skip_comments=False,
        )
        malformed = any(
            item.type == "error"
            or item.type not in {"declaration", "whitespace", "comment"}
            or (item.type == "declaration" and _tokens_have_error(item.value))
            for item in parsed
        )
    except RecursionError:
        raise _fail("invalid_inline_css", resource, node_id=node_id) from None
    if malformed:
        raise _fail("invalid_inline_css", resource, node_id=node_id)

    coverage = _coverage(rules)
    has_theme_declaration = any(rule.declarations for rule in rules)
    demote: list[object] = []
    names: list[str] = []
    for item in parsed:
        if item.type != "declaration":
            continue
        if (
            has_theme_declaration
            and _is_animation_property(item.lower_name)
            and not _is_none(item.value)
        ):
            raise _fail("source_animation", resource, node_id=node_id)
        if not item.important:
            continue
        if item.lower_name == "all":
            if has_theme_declaration:
                raise _fail(
                    "unsupported_source_inline_shorthand",
                    resource,
                    node_id=node_id,
                )
            continue
        intersections = declaration_properties(item.lower_name) & coverage.keys()
        if not intersections:
            continue
        for prop in intersections:
            media = coverage[prop]
            if None not in media and not {"light", "dark"} <= media:
                raise _fail("media_coverage_required", resource, node_id=node_id)
        demote.append(item)
        names.append(item.lower_name)

    if not demote:
        return style, ()
    for declaration in demote:
        declaration.important = False
    try:
        return tinycss2.serialize(parsed), tuple(names)
    except RecursionError:
        raise _fail("invalid_inline_css", resource, node_id=node_id) from None


def _nested_tokens(token: object) -> list[object] | None:
    return getattr(token, "arguments", getattr(token, "content", None))


def _tokens_have_error(tokens: list[object]) -> bool:
    for token in tokens:
        if token.type == "error":
            return True
        nested = _nested_tokens(token)
        if nested is not None and _tokens_have_error(nested):
            return True
    return False


def _count_hashes(tokens: list[object], resource: str) -> int:
    count = 0
    for token in tokens:
        if token.type == "error":
            raise _source_fail("invalid_source_css", resource)
        if token.type == "literal" and token.value == "|":
            raise _source_fail("unsupported_source_selector", resource)
        if token.type == "hash" and token.is_identifier:
            count += 1
        nested = _nested_tokens(token)
        if nested is not None:
            count += _count_hashes(nested, resource)
    return count


def _selector_hashes(tokens: list[object], resource: str) -> int:
    selector = tinycss2.serialize(tokens).strip()
    if not selector:
        raise _source_fail("invalid_source_css", resource)
    hashes = _count_hashes(tokens, resource)
    try:
        soupsieve.compile(selector)
    except (soupsieve.SelectorSyntaxError, NotImplementedError, ValueError):
        raise _source_fail("unsupported_source_selector", resource) from None
    return hashes


def _is_animation_property(name: str) -> bool:
    for prefix in ("-webkit-", "-moz-", "-ms-", "-o-"):
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break
    return name in {"animation", "transition"} or name.startswith(("animation-", "transition-"))


def _is_none(tokens: list[object]) -> bool:
    significant = [token for token in tokens if token.type not in {"whitespace", "comment"}]
    return (
        len(significant) == 1
        and significant[0].type == "ident"
        and significant[0].lower_value == "none"
    )


def _check_source_declarations(tokens: list[object], resource: str) -> None:
    declarations = tinycss2.parse_declaration_list(tokens, skip_whitespace=True, skip_comments=True)
    has_block = any(token.type == "{} block" for token in tokens)
    for declaration in declarations:
        if declaration.type == "error":
            detail = "source_nesting" if has_block else "invalid_source_css"
            raise _source_fail(detail, resource)
        if declaration.type != "declaration":
            raise _source_fail("source_nesting", resource)
        if _tokens_have_error(declaration.value):
            raise _source_fail("invalid_source_css", resource)
        if _is_animation_property(declaration.lower_name) and not _is_none(declaration.value):
            raise _source_fail("source_animation", resource)


def _keyframes_content(tokens: list[object], resource: str) -> None:
    frames = tinycss2.parse_rule_list(tokens, skip_whitespace=True, skip_comments=True)
    for frame in frames:
        if frame.type == "error":
            raise _source_fail("invalid_source_css", resource)
        if frame.type != "qualified-rule":
            raise _source_fail("unsupported_source_rule", resource)
        _check_source_declarations(frame.content, resource)


def _walk_source_rules(
    rules: list[object],
    resource: str,
    *,
    depth: int = 0,
) -> int:
    if depth > _MAX_CONDITIONAL_DEPTH:
        raise _source_fail("invalid_source_css", resource)
    hashes = 0
    for rule in rules:
        if rule.type == "error":
            raise _source_fail("invalid_source_css", resource)
        if rule.type == "qualified-rule":
            hashes += _selector_hashes(rule.prelude, resource)
            _check_source_declarations(rule.content, resource)
            continue
        if rule.type != "at-rule":
            raise _source_fail("unsupported_source_rule", resource)
        if _tokens_have_error(rule.prelude):
            raise _source_fail("invalid_source_css", resource)
        keyword = rule.lower_at_keyword
        if keyword == "layer":
            raise _source_fail("source_layers", resource)
        if keyword == "import":
            raise _source_fail("import_not_resolved", resource)
        if keyword in _CONDITIONAL_RULES:
            if rule.content is None or not any(
                token.type not in {"whitespace", "comment"} for token in rule.prelude
            ):
                raise _source_fail("invalid_source_css", resource)
            nested = tinycss2.parse_rule_list(
                rule.content,
                skip_whitespace=True,
                skip_comments=True,
            )
            hashes += _walk_source_rules(nested, resource, depth=depth + 1)
            continue
        if keyword in {"font-face", "page"}:
            if rule.content is None:
                raise _source_fail("invalid_source_css", resource)
            _check_source_declarations(rule.content, resource)
            continue
        if keyword in {"keyframes", "-webkit-keyframes", "-moz-keyframes", "-o-keyframes"}:
            if rule.content is None:
                raise _source_fail("invalid_source_css", resource)
            _keyframes_content(rule.content, resource)
            continue
        if keyword in _METADATA_RULES and rule.content is None and depth == 0:
            continue
        raise _source_fail("unsupported_source_rule", resource)
    return hashes


def source_specificity_bound(
    stylesheets: Mapping[str, bytes],
    *,
    resource: str = "theme",
) -> int:
    """返回严格高于全部源选择器 ID 数量总和的保守界限。"""
    hashes = 0
    for data in stylesheets.values():
        try:
            rules, _encoding = tinycss2.parse_stylesheet_bytes(
                data,
                skip_whitespace=True,
                skip_comments=True,
            )
            hashes += _walk_source_rules(rules, resource)
        except RecursionError:
            raise _source_fail("invalid_source_css", resource) from None
        if hashes + 1 > _MAX_SPECIFICITY:
            raise _source_fail("specificity_limit", resource)
    return hashes + 1
