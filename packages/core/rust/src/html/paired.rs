//! Backfill canonical standalone HTML and MinerU chapter templates. A declined
//! resource is rendered afresh by the compatibility renderer, never partly native.

use std::collections::{HashMap, HashSet};

use pyo3::prelude::*;
use serde_json::Value;
use sha2::{Digest, Sha256};

use super::{canonical, translatable_attr, ATTRIBUTE};
use crate::epub::{
    dom::{strip, Dom, Id, Kind},
    export::{allocate, backfill, breaks, identities},
    extract::{ANCHOR, ANNOTATION, INLINE, LINE, OBJECT},
};

type Slot = (String, String);
type Edit = (String, String, String, bool, Option<Slot>, Option<String>);

const TEMPORARY: [&str; 5] = [ANCHOR, ANNOTATION, INLINE, LINE, ATTRIBUTE];

// Restrict the canonical check to ordinary HTML, not foreign content, special
// text containers, or arbitrary declaration/processing instruction syntax.
fn ordinary(name: &str) -> bool {
    matches!(
        name,
        "html"
            | "head"
            | "body"
            | "title"
            | "meta"
            | "link"
            | "style"
            | "script"
            | "main"
            | "header"
            | "footer"
            | "nav"
            | "aside"
            | "section"
            | "article"
            | "figure"
            | "figcaption"
            | "div"
            | "p"
            | "blockquote"
            | "address"
            | "h1"
            | "h2"
            | "h3"
            | "h4"
            | "h5"
            | "h6"
            | "ol"
            | "ul"
            | "li"
            | "dl"
            | "dd"
            | "dt"
            | "table"
            | "caption"
            | "colgroup"
            | "col"
            | "thead"
            | "tbody"
            | "tfoot"
            | "tr"
            | "td"
            | "th"
            | "span"
            | "b"
            | "strong"
            | "i"
            | "em"
            | "u"
            | "s"
            | "small"
            | "mark"
            | "abbr"
            | "a"
            | "cite"
            | "q"
            | "time"
            | "sub"
            | "sup"
            | "wbr"
            | "br"
            | "hr"
            | "pre"
            | "code"
            | "kbd"
            | "samp"
            | "var"
            | "bdi"
            | "bdo"
            | "img"
            | "picture"
            | "source"
            | "video"
            | "audio"
            | "track"
            | "form"
            | "label"
            | "button"
            | "input"
            | "select"
            | "optgroup"
            | "option"
    )
}

fn compatible_tree(dom: &Dom) -> bool {
    dom.walk(0).all(|id| match dom.kind(id) {
        Kind::Tag(name, attrs) if ordinary(name) => {
            // BeautifulSoup rewrites declared encodings on serialization and
            // uses Python whitespace rules for tokenized attribute values.
            !attrs
                .values()
                .any(|v| v.contains(['\x1c', '\x1d', '\x1e', '\x1f']))
                && (name != "meta"
                    || (attrs.get("charset").is_none_or(|v| v == "utf-8")
                        && !attrs
                            .get("http-equiv")
                            .is_some_and(|v| v.eq_ignore_ascii_case("content-type"))))
        }
        Kind::Raw(text) => {
            text.starts_with("<!--") || (text == "<!DOCTYPE html>\n" && dom.parent(id) == Some(0))
        }
        Kind::Text(_) => true,
        _ => false,
    })
}

// EPUB's shared renderer agrees with Python for edge objects and validated
// flat placements. HTML's nested ranges, stale placement recovery and
// self-annotating blocks intentionally remain authoritative in Python.
fn compatible_metadata(dom: &Dom, el: Id, source: &str, target: &str, meta: &Value) -> Option<()> {
    let inline = meta.get("epub_inline");
    let ann = meta.get("epub_annotations");
    let mut inline_nodes = HashMap::new();
    let mut roots = HashMap::new();
    for node in dom.tags(el) {
        let inline = dom.attr(node, INLINE);
        let ann = dom.attr(node, ANNOTATION);
        if inline.is_none() && ann.is_none() {
            continue;
        }
        if dom
            .parents(node)
            .take_while(|&n| n != el)
            .any(|n| dom.attr(n, INLINE).is_some() || dom.attr(n, ANNOTATION).is_some())
        {
            return None;
        }
        if inline.is_some_and(|key| inline_nodes.insert(key, node).is_some())
            || ann.is_some_and(|key| roots.insert(key, node).is_some())
        {
            return None;
        }
    }
    let source_length = source.chars().count() as u64;
    let mut objects = HashSet::new();
    if let Some(ann) = ann {
        let length = target.chars().count() as u64;
        if ann["version"].as_u64()? != 1
            || ann["source_length"].as_u64()? != source_length
            || ann["target_digest"].as_str()? != format!("{:x}", Sha256::digest(target.as_bytes()))
        {
            return None;
        }
        let items = ann["items"].as_array()?;
        let placements = ann["placements"].as_array()?;
        if items.len() != placements.len() || items.len() != roots.len() {
            return None;
        }
        let mut by_id = HashMap::new();
        for p in placements {
            if by_id.insert(p["id"].as_str()?, p).is_some()
                || ["status", "method"].iter().any(|key| {
                    let v = &p[*key];
                    !v.is_null()
                        && v.as_str().is_none_or(|s| {
                            matches!(
                                s.to_ascii_lowercase().as_str(),
                                "fallback" | "pending" | "failed" | "invalid" | "missing" | "stale"
                            )
                        })
                })
            {
                return None;
            }
        }
        let mut points = Vec::new();
        let mut ranges = Vec::new();
        for item in items {
            let key = item["id"].as_str()?;
            let node = roots.remove(key)?;
            let p = by_id.remove(key)?;
            let start = p["target_start"].as_u64()?;
            let end = p["target_end"].as_u64()?;
            if end > length
                || !item["html_parent"].is_null()
                || !item["html_tag"].is_null() && item["html_tag"].as_str()? != dom.name(node)
            {
                return None;
            }
            let object = item["relation"].as_str()? == OBJECT;
            match item["mode"].as_str()? {
                "point" if start == end => {
                    if !p["target_order"].is_null() && p["target_order"].as_u64().is_none() {
                        return None;
                    }
                    points.push(start);
                    if object {
                        objects.insert(key);
                    }
                }
                "range" if !object && start < end && item["marker_text"].is_string() => {
                    ranges.push((start, end));
                }
                _ => return None,
            }
        }
        ranges.sort_unstable();
        if ranges.windows(2).any(|r| r[0].1 > r[1].0) {
            return None;
        }
        points.sort_unstable();
        for (start, end) in ranges {
            // Inside a range HTML and EPUB do not preserve the same points.
            let i = points.partition_point(|&p| p <= start);
            if points.get(i).is_some_and(|&p| p < end) {
                return None;
            }
        }
    }
    if let Some(inline) = inline {
        if inline["version"].as_u64()? != 1 || inline["source_length"].as_u64()? != source_length {
            return None;
        }
        for row in inline["nodes"].as_array()? {
            let key = row["id"].as_str()?;
            let node = inline_nodes.remove(key)?;
            if objects.remove(key) {
                if dom.attr(node, ANNOTATION) != Some(key) {
                    return None;
                }
            } else {
                let offset = row["offset"].as_u64()?;
                if dom.attr(node, ANNOTATION).is_some() || (offset != 0 && offset != source_length)
                {
                    return None;
                }
            }
        }
    }
    (roots.is_empty() && inline_nodes.is_empty() && objects.is_empty()).then_some(())
}

fn safe_clone(dom: &Dom, el: Id) -> bool {
    dom.name(el) != "a"
        && std::iter::once(el).chain(dom.tags(el)).all(|node| {
            let Kind::Tag(_, attrs) = dom.kind(node) else {
                unreachable!()
            };
            // Descendant identities and local references require the full
            // Python fragment map, even if the paired block has no IDs itself.
            (node == el || identities(dom, node).next().is_none())
                && attrs.iter().all(|(attr, value)| {
                    !matches!(
                        attr.as_str(),
                        "aria-activedescendant"
                            | "aria-controls"
                            | "aria-describedby"
                            | "aria-details"
                            | "aria-errormessage"
                            | "aria-flowto"
                            | "aria-labelledby"
                            | "aria-owns"
                            | "for"
                            | "form"
                            | "headers"
                            | "itemref"
                            | "list"
                            | "xref"
                    ) && !(matches!(attr.as_str(), "href" | "xlink:href") && value.contains('#'))
                        && !value.contains("url(")
                })
        })
}

#[derive(Clone, Copy)]
enum Pair {
    Line,
    InnerSpan,
    InnerDiv,
    Paragraph,
}

impl Pair {
    fn wrapper(self) -> &'static str {
        match self {
            Self::Line | Self::InnerSpan => "span",
            Self::InnerDiv => "div",
            Self::Paragraph => "p",
        }
    }

    fn insert(self, dom: &mut Dom, el: Id, source: Id, first: bool) {
        match self {
            Self::Line => {
                let br = dom.tag("br");
                dom.insert(el, source, !first);
                dom.insert(if first { source } else { el }, br, true);
            }
            Self::InnerSpan | Self::InnerDiv => {
                let place = if first { Dom::prepend } else { Dom::append };
                // In either order the break goes between the two sides.
                if matches!(self, Self::InnerSpan) {
                    let br = dom.tag("br");
                    place(dom, el, br);
                }
                place(dom, el, source);
            }
            Self::Paragraph => dom.insert(el, source, !first),
        }
    }
}

fn clone_source(
    dom: &mut Dom,
    el: Id,
    pair: Pair,
    source_id: String,
    preserve_style: bool,
    lang: &str,
) -> Id {
    let Kind::Tag(_, attrs) = dom.kind(el) else {
        unreachable!()
    };
    let source = dom.node(Kind::Tag(pair.wrapper().to_owned(), attrs.clone()));
    for child in dom.children(el).to_vec() {
        let child = dom.copy(child);
        dom.append(source, child);
    }
    let nodes: Vec<_> = std::iter::once(source).chain(dom.tags(source)).collect();
    for node in nodes {
        dom.attrs_mut(node)
            .retain(|k, _| !TEMPORARY.contains(&k.as_str()));
    }
    if dom.attr(source, "name").is_some_and(|v| !v.is_empty()) {
        dom.set(source, "name", &source_id);
    }
    dom.set(source, "id", source_id);
    if !lang.is_empty() && dom.attr(source, "lang").is_none() {
        dom.set(source, "lang", lang);
        if dom.attr(source, "xml:lang").is_some() {
            dom.set(source, "xml:lang", lang);
        }
    }
    let classes = if preserve_style {
        match dom.attr(source, "class") {
            Some(v) if v.split(' ').any(|class| class == "tn-source") => v.to_owned(),
            Some(v) if !v.is_empty() => format!("{v} tn-source"),
            _ => "tn-source".to_owned(),
        }
    } else {
        dom.attrs_mut(source).remove("style");
        "tn-source ibooks-dark-theme-use-custom-text-color".to_owned()
    };
    dom.set(source, "class", classes);
    source
}

enum Job {
    Attribute(Id, String, String),
    Block {
        el: Id,
        target: String,
        replace: bool,
        meta: Value,
        pairing: Option<(Pair, Id)>,
    },
}

#[pyfunction]
pub(super) fn html_backfill_paired(
    py: Python<'_>,
    template: &str,
    edits: Vec<Edit>,
    source_first: bool,
    preserve_style: bool,
    source_lang: &str,
) -> Option<String> {
    py.detach(|| render(template, edits, source_first, preserve_style, source_lang))
}

fn render(
    template: &str,
    edits: Vec<Edit>,
    source_first: bool,
    preserve_style: bool,
    source_lang: &str,
) -> Option<String> {
    let mut dom = canonical(template)?;
    if !compatible_tree(&dom) {
        return None;
    }
    let nodes: Vec<_> = dom.tags(0).collect();
    let mut occupied = HashSet::new();
    let mut anchors = HashMap::new();
    let mut slots = HashMap::new();
    for &node in &nodes {
        occupied.extend(identities(&dom, node).map(str::to_owned));
        for (attr, index) in [(ANCHOR, &mut anchors), (ATTRIBUTE, &mut slots)] {
            if let Some(key) = dom.attr(node, attr) {
                if key.is_empty() || index.insert(key.to_owned(), node).is_some() {
                    return None;
                }
            }
        }
        if dom.attr(node, LINE).is_some()
            && (dom.name(node) != "span" || dom.parents(node).any(|n| dom.attr(n, LINE).is_some()))
        {
            return None;
        }
    }

    let mut jobs = Vec::with_capacity(edits.len());
    let mut used_slots = HashSet::new();
    for (anchor, target, source, pair, slot, metadata) in edits {
        if let Some((slot, attr)) = slot {
            let node = *slots.get(&slot)?;
            if anchors.contains_key(&anchor)
                || !translatable_attr(&dom, node, &attr)
                || dom.attr(node, &attr) != Some(&source)
                || !used_slots.insert((node, attr.clone()))
            {
                return None;
            }
            jobs.push(Job::Attribute(node, attr, target));
            continue;
        }
        let el = anchors.remove(&anchor)?;
        if dom.parents(el).any(|n| dom.attr(n, ANCHOR).is_some())
            || dom.tags(el).any(|n| dom.attr(n, ANCHOR).is_some())
        {
            return None;
        }
        let meta: Value = match metadata {
            Some(data) => serde_json::from_str(&data).ok()?,
            None => Value::Null,
        };
        let markup = meta.get("html_markup");
        if markup.is_some_and(|v| v["version"].as_u64() != Some(1)) {
            return None;
        }
        let replace = target != source;
        let line = dom.attr(el, LINE).is_some();
        if replace {
            if dom.attr(el, ANNOTATION).is_some()
                || dom.attr(el, INLINE).is_some()
                || matches!(dom.name(el), "html" | "head" | "body" | "style" | "script")
                || crate::epub::dom::void(dom.name(el))
                || dom
                    .tags(el)
                    .any(|n| matches!(dom.name(n), "style" | "script"))
            {
                return None;
            }
            compatible_metadata(&dom, el, &source, &target, &meta)?;
        }
        // The legacy allocator reserves an ID even for an option whose source
        // copy is then suppressed.
        let pairing = if pair {
            let source_id = allocate(format!("tn-source-{anchor}"), &mut occupied);
            if dom.name(el) == "option" {
                None
            } else {
                if !safe_clone(&dom, el) {
                    return None;
                }
                let placement = if line {
                    Pair::Line
                } else if markup.is_some() && dom.name(el) != "p" {
                    Pair::InnerSpan
                } else if matches!(dom.name(el), "li" | "blockquote") {
                    Pair::InnerDiv
                } else {
                    let parent = dom.parent(el)?;
                    if !matches!(
                        dom.name(parent),
                        "" | "body"
                            | "main"
                            | "section"
                            | "article"
                            | "header"
                            | "footer"
                            | "aside"
                            | "nav"
                            | "div"
                            | "blockquote"
                            | "li"
                            | "dd"
                            | "td"
                            | "th"
                            | "figure"
                            | "figcaption"
                            | "form"
                    ) {
                        return None;
                    }
                    Pair::Paragraph
                };
                let snapshot = clone_source(
                    &mut dom,
                    el,
                    placement,
                    source_id,
                    preserve_style,
                    source_lang,
                );
                Some((placement, snapshot))
            }
        } else {
            None
        };
        jobs.push(Job::Block {
            el,
            target,
            replace,
            meta,
            pairing,
        });
    }
    if !anchors.is_empty() {
        return None;
    }

    // Take every source snapshot before applying even the first attribute or
    // text edit, so bilingual source media keep their original alt/title.
    for job in jobs {
        match job {
            Job::Attribute(node, attr, target) => dom.set(node, &attr, target),
            Job::Block {
                el,
                target,
                replace,
                meta,
                pairing,
            } => {
                if replace {
                    if meta.is_null() {
                        dom.clear(el);
                        breaks(&mut dom, el, &target);
                    } else {
                        let original =
                            if meta.get("html_markup").is_some() && dom.attr(el, LINE).is_some() {
                                dom.text(el, "", false)
                            } else {
                                String::new()
                            };
                        let mut warnings = vec![];
                        backfill(
                            &mut dom,
                            el,
                            &target,
                            &meta,
                            &meta["epub_annotations"],
                            &mut warnings,
                        )
                        .ok()?;
                        if !warnings.is_empty() {
                            return None;
                        }
                        let space = |c: char| strip(c.encode_utf8(&mut [0; 4])).is_empty();
                        let leading = original.len() - original.trim_start_matches(space).len();
                        if leading > 0 {
                            let text = dom.node(Kind::Text(original[..leading].to_owned()));
                            dom.prepend(el, text);
                        }
                        dom.append_text(el, &original[original.trim_end_matches(space).len()..]);
                    }
                }
                dom.attrs_mut(el).remove(ANCHOR);
                if let Some((placement, snapshot)) = pairing {
                    placement.insert(&mut dom, el, snapshot, source_first);
                }
            }
        }
    }
    for node in nodes {
        let attrs = dom.attrs_mut(node);
        for key in [ATTRIBUTE, INLINE, ANNOTATION] {
            attrs.remove(key);
        }
        if attrs.contains_key(LINE) {
            dom.unwrap(node);
        }
    }
    Some(dom.html(0))
}
