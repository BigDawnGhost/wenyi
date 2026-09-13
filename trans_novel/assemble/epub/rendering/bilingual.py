"""Shared schema-4 bilingual DOM contracts for writer and verifier."""

from __future__ import annotations

from lxml import etree

BILINGUAL_SOURCE_CLASS = "tn-source ibooks-dark-theme-use-custom-text-color"
BILINGUAL_SOURCE_CLASSES = frozenset(BILINGUAL_SOURCE_CLASS.split())
# Direct-br target wrappers are generated DOM, not source additions.  The
# marker is deliberately a class (rather than ``data-tn-*``): source roots
# must never carry temporary slot attributes.
BILINGUAL_DIRECT_TARGET_CLASS = "tn-bilingual-target"
BILINGUAL_DIRECT_TARGET_ATTRS = {"class": BILINGUAL_DIRECT_TARGET_CLASS}
BILINGUAL_CONTAINER_TAGS = frozenset({"li", "blockquote", "td", "th", "dt", "dd"})
DIRECT_UNSAFE_ANCESTOR_QNAMES = frozenset({"a", "ruby", "rb", "rt", "rp", "rtc"})
SAFE_SOURCE_INLINE_QNAMES = frozenset(
    {
        "span",
        "em",
        "strong",
        "i",
        "b",
        "u",
        "s",
        "small",
        "big",
        "sub",
        "sup",
        "ruby",
        "rb",
        "rt",
        "rp",
        "rtc",
        "q",
        "cite",
        "code",
        "kbd",
        "samp",
        "var",
        "abbr",
        "time",
        "mark",
        "bdi",
        "bdo",
        "br",
    }
)
RUBY_QNAMES = frozenset({"ruby", "rb", "rt", "rp", "rtc"})
RUBY_ALLOWED_ATTRS = frozenset({"class", "dir", "lang", "title"})
XHTML_NS = "http://www.w3.org/1999/xhtml"


def local_name(tag: object) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def is_bilingual_container_tag(tag: object) -> bool:
    return local_name(tag) in BILINGUAL_CONTAINER_TAGS


def _direct_run_active(node: etree._Element) -> bool:
    name = local_name(node.tag)
    if name in DIRECT_UNSAFE_ANCESTOR_QNAMES or name in {
        "button",
        "datalist",
        "fieldset",
        "form",
        "input",
        "label",
        "option",
        "optgroup",
        "select",
        "textarea",
    }:
        return True
    return any(
        key.lower().startswith("on")
        or local_name(key) in {"action", "formaction", "href", "src", "xlink"}
        for key in node.attrib
    )


def direct_run_boundary(block: etree._Element, owner: etree._Element) -> etree._Element:
    """Return the safe insertion boundary for a direct-br slot."""
    boundary = owner
    current = owner
    while current is not block:
        if _direct_run_active(current):
            boundary = current
        parent = current.getparent()
        if parent is None:
            break
        current = parent
    return boundary


def direct_run_source_copy(
    block: etree._Element,
    owner: etree._Element,
    *,
    source_lang: str,
    source_tag: str,
    source_value: str,
    ruby_source: bool = True,
) -> etree._Element:
    """Create one marked direct source wrapper from the original snapshot."""
    source = etree.Element(source_tag)
    ruby = next(
        (
            node
            for node in (owner, *owner.iterancestors())
            if ruby_source and node is not block and local_name(node.tag) == "ruby"
        ),
        None,
    )
    if ruby is not None and (
        owner is ruby
        or (owner.text or "").strip() == (ruby.text or "").strip()
        or local_name(owner.tag) == "rb"
    ):
        canonical = japanese_ruby_source_copy(ruby, source_lang, "ruby")
        if canonical is None:
            canonical = sanitized_source_copy(ruby, "ruby")
        source.append(canonical)
    else:
        source.text = source_value
    source.set("class", BILINGUAL_SOURCE_CLASS)
    return source


def direct_run_add_whitespace(
    source: etree._Element,
    *,
    prefix: str = "",
    suffix: str = "",
) -> None:
    if prefix:
        source.text = prefix + (source.text or "")
    if suffix:
        if len(source) == 0:
            source.text = (source.text or "") + suffix
        else:
            last = source
            while len(last):
                last = last[-1]
            last.tail = (last.tail or "") + suffix


def ruby_base_count(ruby: etree._Element) -> int:
    """Count visible ruby base cores in implicit and explicit rb forms."""
    count = int(bool((ruby.text or "").strip()))
    for child in ruby:
        if not isinstance(child.tag, str):
            continue
        name = local_name(child.tag)
        if name == "rb":
            count += int(bool((child.text or "").strip()))
        elif name in {"rt", "rp"}:
            count += int(bool((child.tail or "").strip()))
    return count


def direct_run_is_active(node: etree._Element) -> bool:
    return _direct_run_active(node)


def direct_run_has_active_ancestor(block: etree._Element, node: etree._Element) -> bool:
    return any(
        ancestor is not block and _direct_run_active(ancestor) for ancestor in node.iterancestors()
    )


def _is_xhtml_or_unqualified(tag: object) -> bool:
    if not isinstance(tag, str):
        return False
    return "}" not in tag or tag.startswith("{" + XHTML_NS + "}")


def _append_text(parent: etree._Element, value: str | None) -> None:
    if not value:
        return
    if len(parent):
        previous = parent[-1]
        previous.tail = (previous.tail or "") + value
    else:
        parent.text = (parent.text or "") + value


_REMOVED_SOURCE_QNAMES = frozenset(
    {
        "audio",
        "button",
        "canvas",
        "datalist",
        "embed",
        "fieldset",
        "form",
        "hr",
        "iframe",
        "img",
        "input",
        "math",
        "object",
        "optgroup",
        "option",
        "script",
        "select",
        "source",
        "style",
        "svg",
        "textarea",
        "track",
        "video",
    }
)


def _is_removed_source_element(source: etree._Element) -> bool:
    # Unknown namespaces are textual wrappers, not active content.  Preserve
    # their visible descendants while dropping only the explicitly unsafe
    # HTML/SVG/MathML/media/form elements.
    return not isinstance(source.tag, str) or local_name(source.tag) in _REMOVED_SOURCE_QNAMES


def _append_unsupported(parent: etree._Element, source: etree._Element) -> None:
    if _is_removed_source_element(source):
        _append_text(parent, source.tail)
    else:
        _append_flattened(parent, source)
        _append_text(parent, source.tail)


def _copy_attrs(source: etree._Element, target: etree._Element, *, ruby: bool) -> None:
    if not ruby:
        return
    for key, value in source.attrib.items():
        key_local = local_name(key)
        if (
            key_local not in RUBY_ALLOWED_ATTRS
            and key != "{http://www.w3.org/XML/1998/namespace}lang"
        ):
            continue
        if (
            key_local == "lang"
            and key.startswith("{")
            and not key.startswith("{http://www.w3.org/XML/1998/namespace}")
            and not key.startswith("{" + XHTML_NS + "}")
        ):
            # HTML lang is safe; unknown namespaced language is not.
            key = "lang"
        target.set(key, value)


def _copy_safe_element(source: etree._Element) -> etree._Element | None:
    if not isinstance(source.tag, str) or not _is_xhtml_or_unqualified(source.tag):
        return None
    name = local_name(source.tag)
    if name not in SAFE_SOURCE_INLINE_QNAMES:
        return None
    target = etree.Element(source.tag)
    _copy_attrs(source, target, ruby=name in RUBY_QNAMES)
    target.text = source.text
    for child in source:
        if not isinstance(child.tag, str):
            _append_text(target, child.tail)
            continue
        child_copy = _copy_safe_element(child)
        if child_copy is None:
            _append_unsupported(target, child)
        else:
            target.append(child_copy)
            child_copy.tail = child.tail
    return target


def dedupe_segment_mappings(segments: list[object]) -> list[object]:
    """Merge identical logical mappings while rejecting ambiguous coordinates."""
    result: list[object] = []
    identities: dict[tuple[object, tuple[int, ...], tuple[str, ...]], object] = {}
    slot_ids: set[str] = set()
    locations: set[tuple[object, tuple[int, ...], tuple[int, ...], str]] = set()
    for segment in segments:
        state = getattr(segment, "epub_state", None)
        href = getattr(segment, "resource_href", None)
        if state is None or not isinstance(href, str):
            result.append(segment)
            continue
        slots = getattr(state, "slots", ())
        identity = (href, tuple(getattr(state, "block_path", ())), tuple(slot.id for slot in slots))
        prior = identities.get(identity)
        if prior is not None:
            prior_state = getattr(prior, "epub_state", None)
            if (
                getattr(prior, "source", None) != getattr(segment, "source", None)
                or getattr(prior, "target", None) != getattr(segment, "target", None)
                or prior_state != state
            ):
                raise ValueError(f"EPUB duplicate mapping changed source core: {href}")
            continue
        identities[identity] = segment
        for slot in slots:
            if slot.id in slot_ids:
                raise ValueError(f"EPUB duplicate slot id: {href}")
            slot_ids.add(slot.id)
            location = (href, tuple(state.block_path), tuple(slot.element_path), slot.field)
            if location in locations:
                raise ValueError(f"EPUB slot contract overlap: {href}")
            locations.add(location)
        result.append(segment)
    return result


def segment_needs_source(segment: object) -> bool:
    source = getattr(segment, "source", None)
    target = getattr(segment, "target", None)
    return (
        not getattr(segment, "preserve_source", False)
        and getattr(segment, "epub_state", None) is not None
        and getattr(segment, "kind", None) != "heading"
        and isinstance(source, str)
        and bool(source.strip())
        and isinstance(target, str)
        and bool(target.strip())
        and source.strip() != target.strip()
    )


def _append_flattened(parent: etree._Element, source: etree._Element) -> None:
    _append_text(parent, source.text)
    for child in source:
        if not isinstance(child.tag, str):
            _append_text(parent, child.tail)
            continue
        copied = _copy_safe_element(child)
        if copied is None:
            _append_unsupported(parent, child)
        else:
            parent.append(copied)
            copied.tail = child.tail


def sanitized_source_copy(
    original: etree._Element, source_tag: str | None = None
) -> etree._Element:
    """Return a source-only copy with safe inline markup and no active attributes."""
    if not isinstance(original.tag, str):
        raise ValueError("EPUB source block has no element name")
    root_tag = source_tag or original.tag
    root = etree.Element(root_tag)
    root.text = original.text
    for child in original:
        if not isinstance(child.tag, str):
            _append_text(root, child.tail)
            continue
        copied = _copy_safe_element(child)
        if copied is None:
            _append_unsupported(root, child)
        else:
            root.append(copied)
            copied.tail = child.tail
    return root


def _ruby_copy_element(source: etree._Element) -> etree._Element | None:
    if not isinstance(source.tag, str) or not _is_xhtml_or_unqualified(source.tag):
        return None
    name = local_name(source.tag)
    if name not in RUBY_QNAMES | {"br"}:
        return None
    target = etree.Element(source.tag)
    _copy_attrs(source, target, ruby=True)
    target.text = source.text
    for child in source:
        if not isinstance(child.tag, str):
            _append_text(target, child.tail)
            continue
        copied = _ruby_copy_element(child)
        if copied is None:
            _append_ruby_unsupported(target, child)
        else:
            target.append(copied)
            copied.tail = child.tail
    return target


def _append_ruby_flattened(parent: etree._Element, source: etree._Element) -> None:
    _append_text(parent, source.text)
    for child in source:
        if not isinstance(child.tag, str):
            _append_text(parent, child.tail)
            continue
        copied = _ruby_copy_element(child)
        if copied is None:
            _append_ruby_unsupported(parent, child)
        else:
            parent.append(copied)
            copied.tail = child.tail


def japanese_ruby_source_copy(
    original: etree._Element, source_lang: str, source_tag: str | None = None
) -> etree._Element | None:
    """Build canonical Japanese ruby source, or ``None`` for non-ruby sources."""
    normalized = (source_lang or "").strip().replace("_", "-").lower()
    if not (normalized == "ja" or normalized.startswith("ja-")):
        return None
    if not any(
        local_name(node.tag) == "ruby" for node in original.iter() if isinstance(node.tag, str)
    ):
        return None
    root = etree.Element(source_tag or original.tag)
    root.text = original.text
    for child in original:
        if not isinstance(child.tag, str):
            _append_text(root, child.tail)
            continue
        copied = _ruby_copy_element(child)
        if copied is None:
            _append_ruby_unsupported(root, child)
        else:
            root.append(copied)
            copied.tail = child.tail
    return root


def _append_ruby_unsupported(parent: etree._Element, source: etree._Element) -> None:
    if _is_removed_source_element(source):
        _append_text(parent, source.tail)
    else:
        _append_ruby_flattened(parent, source)
        _append_text(parent, source.tail)


def ruby_shape_is_valid(node: etree._Element) -> bool:
    for ruby in node.iter():
        if not isinstance(ruby.tag, str) or local_name(ruby.tag) != "ruby":
            continue
        for child in ruby:
            if not isinstance(child.tag, str):
                continue
            if local_name(child.tag) == "ruby":
                return False
            if local_name(child.tag) in {"rt", "rp"} and any(
                isinstance(descendant.tag, str) for descendant in child.iterdescendants()
            ):
                return False
    return True


def source_node_is_valid(node: etree._Element) -> bool:
    if (
        not isinstance(node.tag, str)
        or not _is_xhtml_or_unqualified(node.tag)
        or local_name(node.tag) not in {"p", "div", "span"}
        or not ruby_shape_is_valid(node)
    ):
        return False
    for child in node.iter():
        if not isinstance(child.tag, str) or child is node:
            continue
        if (
            not _is_xhtml_or_unqualified(child.tag)
            or local_name(child.tag) not in SAFE_SOURCE_INLINE_QNAMES
        ):
            return False
        if local_name(child.tag) in RUBY_QNAMES:
            if any(
                key not in RUBY_ALLOWED_ATTRS
                and key != "{http://www.w3.org/XML/1998/namespace}lang"
                for key in child.attrib
            ):
                return False
        elif child.attrib:
            return False
    return True


def has_reserved_source_collision(root: etree._Element) -> bool:
    for node in root.iter():
        if not isinstance(node.tag, str):
            continue
        if any(key.lower().startswith("data-tn-") for key in node.attrib):
            return True
        classes = str(node.get("class", "")).split()
        if "tn-source" in classes or BILINGUAL_DIRECT_TARGET_CLASS in classes:
            return True
    return False


__all__ = [
    "BILINGUAL_CONTAINER_TAGS",
    "BILINGUAL_DIRECT_TARGET_ATTRS",
    "BILINGUAL_DIRECT_TARGET_CLASS",
    "BILINGUAL_SOURCE_CLASS",
    "BILINGUAL_SOURCE_CLASSES",
    "DIRECT_UNSAFE_ANCESTOR_QNAMES",
    "RUBY_ALLOWED_ATTRS",
    "SAFE_SOURCE_INLINE_QNAMES",
    "dedupe_segment_mappings",
    "direct_run_add_whitespace",
    "direct_run_boundary",
    "direct_run_has_active_ancestor",
    "is_bilingual_container_tag",
    "japanese_ruby_source_copy",
    "local_name",
    "ruby_base_count",
    "ruby_shape_is_valid",
    "sanitized_source_copy",
]
