//! Template backfill and ZIP publication payloads.

use std::{
    borrow::Cow,
    collections::{HashMap, HashSet},
    ops,
    path::Path,
    sync::LazyLock,
};

use anyhow::{anyhow, bail, Result};
use indexmap::IndexMap;
use regex::Regex;
use serde::Deserialize;
use serde_json::Value;

use crate::alignment::target_digest;

use super::{
    archive::{open, read, rewrite, Archive},
    dom::{decode, decode_xml, escape, strip, Dom, Id, Kind},
    extract::{annotate, FragmentIndex, Resource, ANCHOR, ANNOTATION, INLINE, LINE, OBJECT},
    ingest::{nav_headings, nav_labels, ncx_points, xml, Spec},
    links::{is_html, resolve},
    tokenizer::xml_encoding,
    workers::ordered_map,
};

#[derive(Deserialize)]
pub struct Options {
    pub title: String,
    pub source_lang: String,
    pub bilingual: bool,
    pub source_first: bool,
    pub preserve_source_style: bool,
    pub resources: Vec<Spec>,
    pub toc_paths: HashSet<String>,
    pub toc_entries: Vec<NavEntry>,
    pub segments: Vec<Saved>,
    pub presentation: Option<Presentation>,
    #[serde(default)]
    pub package_path: Option<String>,
    #[serde(default)]
    pub about: Option<String>,
    #[serde(default)]
    pub resource_titles: HashMap<String, String>,
}

#[derive(Deserialize)]
pub struct Presentation {
    pub lang: String,
    pub title: String,
    pub css: String,
    #[serde(default)]
    pub horizontal: bool,
    #[serde(default)]
    pub original_title: Option<String>,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub subjects: Vec<String>,
    /// Translated `dc:creator` values, aligned by position with the non-empty source creators.
    #[serde(default)]
    pub creators: Vec<String>,
    /// Source navigation-document labels ("目录") mapped to their translations.
    #[serde(default)]
    pub nav_titles: HashMap<String, String>,
}

#[derive(Deserialize)]
pub struct NavEntry {
    toc_path: String,
    node_index: usize,
    raw_href: Option<String>,
    title: Option<String>,
    title_translated: Option<String>,
    kind: Option<String>,
}

impl NavEntry {
    fn title(&self, href: &str) -> Option<&str> {
        if self.raw_href.as_deref().is_some_and(|s| s != href) {
            return None;
        }
        self.title_translated
            .as_deref()
            .filter(|s| !s.is_empty())
            .or(self.title.as_deref())
            .map(strip)
            .filter(|s| !s.is_empty())
    }
}

#[derive(Deserialize)]
pub struct Saved {
    pub resource_href: String,
    pub anchor: String,
    pub source: String,
    pub target: String,
    pub heading: bool,
    pub annotations: Value,
}

struct Block {
    saved: Saved,
    fresh: usize,
    clone: Option<String>,
    local: IndexMap<String, String>,
}

struct Page {
    href: String,
    content: Resource,
    blocks: Vec<Block>,
}

static RUBY: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"〘[^〙]*〙").unwrap());
static URL: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r#"url\(\s*(['"]?)#([^)'"\s]+)['"]?\s*\)"#).unwrap());
static VERTICAL: LazyLock<regex::bytes::Regex> = LazyLock::new(|| {
    regex::bytes::Regex::new(
    r#"(?i)(?:(?:-epub-|-webkit-)?writing-mode\s*:\s*(?:vertical-rl|vertical-lr|tb-rl)|page-progression-direction\s*=\s*['"]rtl['"]|\bclass\s*=\s*['"][^'"]*\bvrtl\b)"#
).unwrap()
});

fn legacy_xml_encoding(text: &str) -> Option<ops::Range<usize>> {
    xml_encoding(text.as_bytes()).filter(|range| !text[range.clone()].eq_ignore_ascii_case("utf-8"))
}

fn decode_xhtml_for_export(bytes: &[u8]) -> Cow<'_, str> {
    let mut text = decode(bytes);
    // Fix the real prolog before parsing; the tolerant DOM represents several
    // kinds of markup as raw strings, which must not be searched and replaced.
    if let Some(range) = legacy_xml_encoding(&text) {
        text.to_mut().replace_range(range, "utf-8");
    }
    text
}

fn has_source(s: &Saved) -> bool {
    if s.heading || s.source == s.target || strip(&s.target).is_empty() {
        return false;
    }
    let text = RUBY.replace_all(&s.source, |m: &regex::Captures<'_>| {
        if m[0].starts_with("〘object:") {
            m[0].to_owned()
        } else {
            String::new()
        }
    });
    !strip(&text).is_empty() && text != s.target
}

pub(crate) fn allocate(base: String, occupied: &mut HashSet<String>) -> String {
    if occupied.insert(base.clone()) {
        return base;
    }
    for i in 2.. {
        let id = format!("{base}-{i}");
        if occupied.insert(id.clone()) {
            return id;
        }
    }
    unreachable!()
}

pub(crate) fn identities(dom: &Dom, node: Id) -> impl Iterator<Item = &str> {
    ["id", "name"]
        .into_iter()
        .filter_map(move |a| dom.attr(node, a))
        .filter(|s| !s.is_empty())
}

fn prepare(spec: Spec, bytes: Vec<u8>, stored: Vec<Saved>, options: &Options) -> Result<Page> {
    let href = spec.href;
    let text = if options.presentation.is_some() {
        decode_xhtml_for_export(&bytes)
    } else {
        decode(&bytes)
    };
    let content = annotate(
        &text,
        spec.index,
        &href,
        &options.title,
        options.toc_paths.contains(&href),
    );
    let by_id: HashMap<_, _> = content
        .segments
        .iter()
        .enumerate()
        .map(|(i, seg)| (seg.anchor.as_str(), i))
        .collect();
    let dom = &content.dom;
    let mut occupied = if options.bilingual {
        dom.tags(0)
            .flat_map(|n| identities(dom, n))
            .map(str::to_owned)
            .collect()
    } else {
        HashSet::new()
    };
    let mut blocks = Vec::with_capacity(stored.len());
    for saved in stored {
        let Some(&fresh) = by_id.get(saved.anchor.as_str()) else {
            bail!(
                "EPUB body does not match translation state: {href} lacks backfill anchors {}",
                saved.anchor
            );
        };
        if content.segments[fresh].source != saved.source {
            bail!(
                "EPUB source does not match translation state: {href} content changed ({})",
                saved.anchor
            );
        }
        let clone = (options.bilingual && has_source(&saved))
            .then(|| allocate(format!("tn-source-{}", saved.anchor), &mut occupied));
        blocks.push(Block {
            saved,
            fresh,
            clone,
            local: IndexMap::new(),
        });
    }
    for block in &mut blocks {
        let Some(clone) = &block.clone else {
            continue;
        };
        let el = content.segments[block.fresh].node;
        for (ordinal, node) in std::iter::once(el).chain(dom.tags(el)).enumerate() {
            let values: Vec<_> = identities(dom, node).collect();
            if values.is_empty() {
                continue;
            }
            let id = if node == el && dom.name(el) != "a" {
                clone.clone()
            } else {
                allocate(format!("{clone}-fragment-{ordinal}"), &mut occupied)
            };
            for value in values {
                block
                    .local
                    .entry(value.to_owned())
                    .or_insert_with(|| id.clone());
            }
        }
    }
    Ok(Page {
        href,
        content,
        blocks,
    })
}

type LinkMap = HashMap<String, HashMap<String, String>>;

fn sources(pages: &[Page]) -> LinkMap {
    let mut links = HashMap::new();
    for page in pages {
        let dom = &page.content.dom;
        let by_node: HashMap<_, _> = page
            .blocks
            .iter()
            .filter_map(|b| Some((page.content.segments[b.fresh].node, b.clone.as_ref()?)))
            .collect();
        let mut local = HashMap::new();
        for (fragment, node) in FragmentIndex::new(dom).boundaries {
            if let Some(new) = node.and_then(|id| by_node.get(&id)) {
                local.insert(fragment.to_owned(), (*new).clone());
            }
        }
        for block in &page.blocks {
            local.extend(block.local.iter().map(|(a, b)| (a.clone(), b.clone())));
        }
        if !local.is_empty() {
            links.insert(page.href.clone(), local);
        }
    }
    links
}

fn snapshot(dom: &mut Dom, node: Id, href: &str, links: &LinkMap, block: &Block) -> Id {
    let root = dom.copy(node);
    let nodes: Vec<_> = std::iter::once(root).chain(dom.tags(root)).collect();
    let local = |id: &str| {
        block
            .local
            .get(id)
            .or_else(|| links.get(href).and_then(|m| m.get(id)))
    };
    for id in nodes {
        let attrs = dom.attrs_mut(id);
        attrs.retain(|key, value| {
            match key.as_str() {
                ANCHOR | ANNOTATION | INLINE | LINE | "data-tn-attribute-id" => return false,
                "id" | "name" => {
                    if let Some(new) = block.local.get(value) {
                        value.clone_from(new);
                    }
                }
                "href" | "xlink:href" => {
                    if let Some((prefix, _)) = value.split_once('#') {
                        let link = resolve(href, value);
                        if !link.external && !link.fragment.is_empty() {
                            let found = if link.path == href {
                                local(&link.fragment)
                            } else {
                                links.get(&link.path).and_then(|m| m.get(&link.fragment))
                            };
                            if let Some(new) = found {
                                *value = format!("{prefix}#{new}");
                            }
                        }
                    }
                }
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
                | "xref" => {
                    *value = value
                        .split_whitespace()
                        .map(|s| local(s).map_or(s, String::as_str))
                        .collect::<Vec<_>>()
                        .join(" ");
                }
                _ => {}
            }
            if value.contains("url(") {
                if let std::borrow::Cow::Owned(new) =
                    URL.replace_all(value, |m: &regex::Captures<'_>| {
                        local(&m[2]).map_or_else(
                            || m[0].to_owned(),
                            |new| format!("url({}#{new}{})", &m[1], &m[1]),
                        )
                    })
                {
                    *value = new;
                }
            }
            true
        });
    }
    root
}

fn range_markers(dom: &mut Dom, root: Id, text: &str) -> Vec<Id> {
    if text.is_empty() {
        return vec![];
    }
    let mut candidate = None;
    let mut simple = None;
    for id in dom.tags(root) {
        if matches!(dom.name(id), "sup" | "sub") && dom.text(id, "", true) == text {
            candidate = Some(id);
        }
        if dom.tags(id).next().is_none() && dom.text(id, "", true) == text {
            simple = Some(id);
        }
    }
    candidate.or(simple).map_or_else(Vec::new, |id| {
        dom.detach(id);
        vec![id]
    })
}

fn fallback(dom: &mut Dom, node: Id, point: bool, text: &str, markers: Option<Vec<Id>>) {
    if point {
        return;
    }
    let markers = markers.unwrap_or_else(|| range_markers(dom, node, text));
    dom.clear(node);
    if markers.is_empty() {
        dom.append_text(node, if text.is_empty() { "↩" } else { text });
    } else {
        for id in markers {
            dom.append(node, id);
        }
    }
}

struct Point {
    at: usize,
    order: usize,
    node: Id,
    object: bool,
    rank: Option<u64>,
}
struct Range<'a> {
    start: usize,
    end: usize,
    order: usize,
    node: Id,
    markers: Vec<Id>,
    text: &'a str,
}

pub(crate) fn breaks(dom: &mut Dom, root: Id, text: &str) {
    if !text.contains(['\r', '\n']) {
        dom.append_text(root, text);
        return;
    }
    let text = text.replace("\r\n", "\n").replace('\r', "\n");
    for (i, line) in text.split('\n').enumerate() {
        if i > 0 {
            let br = dom.tag("br");
            dom.append(root, br);
        }
        dom.append_text(root, line);
    }
}

pub(crate) fn backfill(
    dom: &mut Dom,
    el: Id,
    text: &str,
    fresh: &Value,
    stored: &Value,
    warnings: &mut Vec<String>,
) -> Result<()> {
    let inline = &fresh["epub_inline"];
    let ann = &fresh["epub_annotations"];
    let records = inline["nodes"].as_array().map_or(&[][..], Vec::as_slice);
    let items = ann["items"]
        .as_array()
        .or_else(|| stored["items"].as_array())
        .map_or(&[][..], Vec::as_slice);
    if records.is_empty() && items.is_empty() {
        dom.clear(el);
        breaks(dom, el, text.trim_matches(['\r', '\n']));
        return Ok(());
    }
    let offsets: Vec<_> = text
        .char_indices()
        .map(|(i, _)| i)
        .chain(std::iter::once(text.len()))
        .collect();
    let placements: HashMap<_, _> = stored["placements"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|p| Some((p["id"].as_str()?, p)))
        .collect();
    let correct = stored["target_digest"]
        .as_str()
        .is_some_and(|expected| expected == target_digest(text));
    let objects: HashSet<_> = items
        .iter()
        .filter(|v| v["relation"] == OBJECT)
        .filter_map(|v| v["id"].as_str())
        .collect();
    if let Some(self_id) = dom.attr(el, ANNOTATION) {
        let marker = items
            .iter()
            .find(|v| v["id"] == self_id)
            .and_then(|v| v["marker_text"].as_str())
            .unwrap_or("");
        let markers = range_markers(dom, el, marker);
        dom.attrs_mut(el).remove(ANNOTATION);
        // The node itself is not part of the annotation/root index below.
        return replace(
            dom,
            el,
            text,
            offsets.as_slice(),
            records,
            &inline["source_length"],
            items,
            ann,
            stored,
            correct,
            &placements,
            &objects,
            markers,
            warnings,
        );
    }
    replace(
        dom,
        el,
        text,
        offsets.as_slice(),
        records,
        &inline["source_length"],
        items,
        ann,
        stored,
        correct,
        &placements,
        &objects,
        vec![],
        warnings,
    )
}

#[allow(clippy::too_many_arguments)]
fn replace<'a>(
    dom: &mut Dom,
    el: Id,
    text: &str,
    offsets: &[usize],
    records: &[Value],
    source_length: &Value,
    items: &'a [Value],
    ann: &Value,
    stored: &Value,
    correct: bool,
    placements: &HashMap<&str, &Value>,
    objects: &HashSet<&str>,
    self_markers: Vec<Id>,
    warnings: &mut Vec<String>,
) -> Result<()> {
    let length = offsets.len() - 1;
    let inline_length = source_length.as_u64().unwrap_or(0);
    let mut fixed = Vec::new();
    for (order, record) in records.iter().enumerate() {
        let Some(id) = record["id"].as_str() else {
            continue;
        };
        if objects.contains(id) {
            continue;
        }
        let Some(node) = dom.tags(el).find(|&n| dom.attr(n, INLINE) == Some(id)) else {
            continue;
        };
        let pos = record["offset"].as_u64().unwrap_or(0);
        let at = if pos == 0 {
            0
        } else if pos >= inline_length {
            length
        } else {
            bail!("Protected object {id} has pending alignment");
        };
        dom.detach(node);
        dom.attrs_mut(node).remove(INLINE);
        fixed.push(Point {
            at,
            order: items.len() + records.len() + order,
            node,
            object: false,
            rank: None,
        });
    }
    let roots: HashMap<_, _> = dom
        .tags(el)
        .filter_map(|n| Some((dom.attr(n, ANNOTATION)?.to_owned(), n)))
        .collect();
    let mut points = Vec::new();
    let mut ranges = Vec::new();
    let mut fallbacks = Vec::new();
    let invalid = |v: &Value| {
        matches!(
            v.as_str().unwrap_or("").to_ascii_lowercase().as_str(),
            "fallback" | "pending" | "failed" | "invalid" | "missing" | "stale"
        )
    };
    for (order, item) in items.iter().enumerate() {
        let Some(id) = item["id"].as_str() else {
            continue;
        };
        let Some(&node) = roots.get(id) else {
            continue;
        };
        let point = item["mode"] == "point";
        let object = item["relation"] == OBJECT;
        let marker = item["marker_text"].as_str().unwrap_or("");
        let placement = placements.get(id).copied().unwrap_or(&Value::Null);
        let at = placement["target_start"]
            .as_u64()
            .zip(placement["target_end"].as_u64())
            .filter(|&(s, e)| {
                correct
                    && s <= e
                    && e <= length as u64
                    && !invalid(&placement["status"])
                    && !invalid(&placement["method"])
            });
        dom.detach(node);
        match at.map(|(s, e)| (s as usize, e as usize)) {
            Some((s, e)) if point && s == e => points.push(Point {
                at: s,
                order,
                node,
                object,
                rank: placement["target_order"].as_u64(),
            }),
            Some((s, e)) if !point && s < e => {
                let markers = range_markers(dom, node, marker);
                ranges.push(Range {
                    start: s,
                    end: e,
                    order,
                    node,
                    markers,
                    text: marker,
                });
            }
            None if object => warnings.push(id.to_owned()),
            None if !point
                && marker.is_empty()
                && items.len() == 1
                && item["source_start"] == 0
                && item["source_end"].as_u64().is_some_and(|e| {
                    e > 0
                        && Some(e)
                            == ann["source_length"]
                                .as_u64()
                                .or_else(|| stored["source_length"].as_u64())
                }) =>
            {
                ranges.push(Range {
                    start: 0,
                    end: length,
                    order,
                    node,
                    markers: vec![],
                    text: marker,
                });
            }
            _ => {
                fallback(dom, node, point, marker, None);
                fallbacks.push(node);
            }
        }
    }
    ranges.sort_by_key(|r| (r.start, r.end, r.order));
    let mut end = 0;
    ranges.retain_mut(|r| {
        if r.start < end {
            fallback(
                dom,
                r.node,
                false,
                r.text,
                Some(std::mem::take(&mut r.markers)),
            );
            fallbacks.push(r.node);
            false
        } else {
            end = r.end;
            true
        }
    });
    let mut filtered = Vec::new();
    for p in points {
        let inside = ranges.iter().any(|r| r.start < p.at && p.at < r.end);
        if inside && p.object && dom.name(p.node) == "a" {
            bail!(
                "Protected object {} has conflicting link alignment",
                items[p.order]["id"]
            );
        }
        if inside && !p.object {
            fallbacks.push(p.node);
        } else {
            filtered.push(p);
        }
    }
    let mut group: HashMap<_, Vec<_>> = HashMap::new();
    for (i, p) in filtered.iter().enumerate() {
        if let Some(rank) = p.rank {
            group.entry(p.at).or_default().push((i, rank, p.node));
        }
    }
    for slots in group.values() {
        let mut ordered = slots.clone();
        ordered.sort_by_key(|s| s.1);
        for (old, new) in slots.iter().zip(ordered) {
            filtered[old.0].node = new.2;
        }
    }
    filtered.extend(fixed);
    filtered.sort_by_key(|p| (p.at, p.order));
    dom.clear(el);
    let mut pts = filtered.into_iter().peekable();
    let append_until =
        |dom: &mut Dom,
         to,
         mut cursor: usize,
         limit: usize,
         include: bool,
         pts: &mut std::iter::Peekable<std::vec::IntoIter<Point>>| {
            while pts
                .peek()
                .is_some_and(|p| p.at < limit || include && p.at == limit)
            {
                let p = pts.next().unwrap();
                let at = p.at.clamp(cursor, limit);
                breaks(dom, to, &text[offsets[cursor]..offsets[at]]);
                dom.append(to, p.node);
                cursor = at;
            }
            breaks(dom, to, &text[offsets[cursor]..offsets[limit]]);
        };
    let mut cursor = 0;
    for r in ranges {
        append_until(dom, el, cursor, r.start, true, &mut pts);
        dom.clear(r.node);
        append_until(dom, r.node, r.start, r.end, false, &mut pts);
        for marker in r.markers {
            dom.append(r.node, marker);
        }
        dom.append(el, r.node);
        cursor = r.end;
    }
    append_until(dom, el, cursor, length, true, &mut pts);
    for (i, node) in fallbacks.into_iter().enumerate() {
        if i > 0 {
            dom.append_text(el, "、");
        }
        dom.append(el, node);
    }
    for node in self_markers {
        dom.append(el, node);
    }
    Ok(())
}

fn rewrite_nav(dom: &mut Dom, path: &str, options: &Options) {
    // Declared navigation documents carry their page labels whatever their name or marker.
    if let Some(p) = options
        .presentation
        .as_ref()
        .filter(|p| !p.nav_titles.is_empty() && options.toc_paths.contains(path))
    {
        for node in nav_headings(dom) {
            let source = dom.text(node, " ", true);
            // An unchanged label (fallback or identical translation) is left alone, so a
            // language declared on it or a wrapper inside the nav still applies.
            if let Some(title) = p
                .nav_titles
                .get(&source)
                .map(|s| strip(s))
                .filter(|s| !s.is_empty() && *s != source)
            {
                let title = title.to_owned();
                replace_label(dom, node, &title, &p.lang);
            }
        }
    }
    let name = path.to_ascii_lowercase();
    if !name.contains("nav")
        && !name.contains("toc")
        && !dom.tags(0).any(|n| {
            dom.name(n) == "nav" && {
                dom.attr(n, "role") == Some("doc-toc")
                    || ["epub:type", "type", "role"]
                        .into_iter()
                        .find_map(|a| dom.attr(n, a).filter(|s| !s.is_empty()))
                        .is_some_and(|s| s.split_whitespace().any(|s| s == "toc"))
            }
        })
    {
        return;
    }
    let entries: HashMap<_, _> = options
        .toc_entries
        .iter()
        .filter(|e| e.toc_path == path)
        .map(|e| (e.node_index, e))
        .collect();
    if entries.is_empty() {
        return;
    }
    for (i, (_, label, ..)) in nav_labels(dom).into_iter().enumerate() {
        if let Some(title) = entries
            .get(&i)
            .and_then(|e| e.title(dom.attr(label, "href").unwrap_or("")))
        {
            dom.clear(label);
            dom.append_text(label, title);
        }
    }
}

/// Replace a label's text while keeping its anchors and wrapper markup.
///
/// The text goes into the innermost element of a single-element chain (`<h1><span id>…`);
/// empty elements such as `<a id/>` anchors stay in place. A label whose text spans several
/// elements is left untouched rather than flattened. Elements on the chain that declare a
/// language are relabelled with the target language.
fn replace_label(dom: &mut Dom, node: Id, title: &str, lang: &str) {
    let mut chain = vec![node];
    let mut target = node;
    loop {
        let children = dom.children(target).to_vec();
        let filled: Vec<Id> = children
            .iter()
            .copied()
            .filter(|&c| !dom.name(c).is_empty() && dom.meaningful(c))
            .collect();
        let loose_text = children
            .iter()
            .any(|&c| matches!(dom.kind(c), Kind::Text(t) if !strip(t).is_empty()));
        match filled.as_slice() {
            [] => {
                for child in children {
                    if matches!(dom.kind(child), Kind::Text(_)) {
                        dom.detach(child);
                    }
                }
                dom.append_text(target, title);
                break;
            }
            [only] if !loose_text => {
                target = *only;
                chain.push(target);
            }
            _ => return,
        }
    }
    let mut declared = false;
    for &id in &chain {
        for key in ["lang", "xml:lang"] {
            if dom.attr(id, key).is_some() {
                dom.set(id, key, lang.to_owned());
                declared = true;
            }
        }
    }
    // A label inheriting another language from <head> or <nav> declares the target itself.
    let inherited = dom
        .parents(node)
        .find_map(|a| dom.attr(a, "xml:lang").or_else(|| dom.attr(a, "lang")));
    if !declared && inherited.is_some_and(|value| !same_language(value, lang)) {
        dom.set(node, "lang", lang.to_owned());
        dom.set(node, "xml:lang", lang.to_owned());
    }
}

pub fn style(dom: &mut Dom, id: &str, css: &str, html: Id) {
    if dom.tags(0).any(|n| dom.attr(n, "id") == Some(id)) {
        return;
    }
    let head = dom.find(0, "head").unwrap_or_else(|| {
        let head = dom.tag("head");
        dom.prepend(html, head);
        head
    });
    let node = dom.tag("style");
    dom.set(node, "id", id);
    dom.append_text(node, css);
    dom.append(head, node);
}

const HORIZONTAL: &str = "html, body { writing-mode: horizontal-tb !important; -epub-writing-mode: horizontal-tb !important; -webkit-writing-mode: horizontal-tb !important; direction: ltr !important; text-orientation: mixed !important; } .vrtl, .vertical, [class*=\"vrtl\"] { writing-mode: horizontal-tb !important; -epub-writing-mode: horizontal-tb !important; -webkit-writing-mode: horizontal-tb !important; direction: ltr !important; }";

fn present(dom: &mut Dom, href: &str, p: &Presentation, options: &Options) -> bool {
    let Some(html) = dom.find(0, "html") else {
        return false;
    };
    dom.set(html, "lang", p.lang.clone());
    dom.set(html, "xml:lang", p.lang.clone());
    if let Some(title) = options
        .resource_titles
        .get(href)
        .map(|s| strip(s))
        .filter(|s| !s.is_empty())
    {
        let head = dom.direct(html, "head").unwrap_or_else(|| {
            let head = dom.tag("head");
            dom.prepend(html, head);
            head
        });
        let title_node = dom.direct(head, "title").unwrap_or_else(|| {
            let title_node = dom.tag("title");
            dom.prepend(head, title_node);
            title_node
        });
        if strip(&dom.text(title_node, "", false)) != title {
            dom.clear(title_node);
            dom.append_text(title_node, title);
            dom.set(title_node, "lang", p.lang.clone());
            dom.set(title_node, "xml:lang", p.lang.clone());
        }
    }
    if let Some(classes) = dom
        .attr(html, "class")
        .filter(|s| s.split_whitespace().any(|class| class == "vrtl"))
    {
        let kept = classes
            .split_whitespace()
            .filter(|&s| s != "vrtl")
            .collect::<Vec<_>>()
            .join(" ");
        dom.set(html, "class", kept);
    }
    if p.horizontal {
        style(dom, "trans-novel-horizontal-override", HORIZONTAL, html);
    }
    if options.bilingual && !options.preserve_source_style {
        style(dom, "tn-bilingual-style", &p.css, html);
    }
    true
}

fn output<T>(
    mut page: Page,
    links: &LinkMap,
    options: &Options,
    finish: &impl Fn(Dom, bool) -> T,
) -> Result<(String, T, Vec<String>)> {
    let dom = &mut page.content.dom;
    let snapshots: Vec<_> = page
        .blocks
        .iter()
        .map(|b| {
            b.clone.as_ref().map(|_| {
                snapshot(
                    dom,
                    page.content.segments[b.fresh].node,
                    &page.href,
                    links,
                    b,
                )
            })
        })
        .collect();
    let mut warnings = Vec::new();
    for (block, snap) in page.blocks.iter().zip(snapshots) {
        let seg = &page.content.segments[block.fresh];
        let node = seg.node;
        if block.saved.target != block.saved.source {
            backfill(
                dom,
                node,
                &block.saved.target,
                &seg.meta,
                &block.saved.annotations,
                &mut warnings,
            )?;
        }
        dom.attrs_mut(node).remove(ANCHOR);
        if let (Some(src_id), Some(snap)) = (&block.clone, snap) {
            if dom.name(node) == "option" {
                continue;
            }
            let line = dom.attr(node, LINE).is_some();
            // Keep the copy inside structural containers: rows admit only cells,
            // and a figure's caption must remain its first or last child.
            let nested = matches!(
                dom.name(node),
                "li" | "blockquote" | "td" | "th" | "figcaption"
            );
            let copy = dom.tag(if line {
                "span"
            } else if nested {
                "div"
            } else {
                "p"
            });
            if dom.name(snap) != "a" {
                let Kind::Tag(_, attrs) = dom.kind(snap) else {
                    unreachable!()
                };
                *dom.attrs_mut(copy) = attrs.clone();
            }
            if matches!(dom.name(node), "td" | "th") {
                for attr in ["colspan", "rowspan", "headers", "scope", "abbr"] {
                    dom.attrs_mut(copy).remove(attr);
                }
            }
            if !options.source_lang.is_empty() && dom.attr(copy, "lang").is_none() {
                dom.set(copy, "lang", options.source_lang.clone());
                if dom.attr(copy, "xml:lang").is_some() {
                    dom.set(copy, "xml:lang", options.source_lang.clone());
                }
            }
            let classes = if options.preserve_source_style {
                let mut classes = dom.attr(copy, "class").unwrap_or("").to_owned();
                if !classes.split_whitespace().any(|s| s == "tn-source") {
                    if !classes.is_empty() {
                        classes.push(' ');
                    }
                    classes.push_str("tn-source");
                }
                classes
            } else {
                dom.attrs_mut(copy).remove("style");
                "tn-source ibooks-dark-theme-use-custom-text-color".to_owned()
            };
            dom.set(copy, "id", src_id.clone());
            dom.set(copy, "class", classes);
            if dom.name(snap) == "a" {
                dom.append(copy, snap);
            } else {
                for child in dom.children(snap).to_vec() {
                    dom.append(copy, child);
                }
            }
            if line {
                let br = dom.tag("br");
                if options.source_first {
                    dom.insert(node, copy, false);
                    dom.insert(copy, br, true);
                } else {
                    dom.insert(node, copy, true);
                    dom.insert(node, br, true);
                }
            } else if nested {
                if options.source_first {
                    dom.prepend(node, copy);
                } else {
                    dom.append(node, copy);
                }
            } else {
                dom.insert(node, copy, !options.source_first);
            }
        }
    }
    let nodes: Vec<_> = dom.tags(0).collect();
    for n in nodes {
        if dom.attr(n, LINE).is_some() {
            dom.unwrap(n);
        }
        for attr in [ANCHOR, INLINE, ANNOTATION, "data-tn-attribute-id"] {
            dom.attrs_mut(n).remove(attr);
        }
    }
    let presented = if let Some(p) = &options.presentation {
        rewrite_nav(dom, &page.href, options);
        present(dom, &page.href, p, options)
    } else {
        false
    };
    Ok((page.href, finish(page.content.dom, presented), warnings))
}

pub fn resources(
    zip: &mut Archive,
    options: &mut Options,
) -> Result<(IndexMap<String, String>, Vec<String>)> {
    resource_pages(zip, options, |dom, _| dom.epub_html())
}

pub(super) fn resource_pages<T: Send>(
    zip: &mut Archive,
    options: &mut Options,
    finish: impl Fn(Dom, bool) -> T + Sync,
) -> Result<(IndexMap<String, T>, Vec<String>)> {
    let mut groups: IndexMap<String, Vec<Saved>> = IndexMap::new();
    for seg in std::mem::take(&mut options.segments) {
        groups
            .entry(seg.resource_href.clone())
            .or_default()
            .push(seg);
    }
    if options.resources.is_empty() || groups.is_empty() {
        return Ok((IndexMap::new(), vec![]));
    }
    let inputs = options
        .resources
        .iter()
        .filter_map(|s| {
            groups.shift_remove(&s.href).map(|saved| {
                if zip.index_for_name(&s.href).is_none() {
                    bail!("EPUB body resource not found: {}", s.href);
                }
                Ok((s.clone(), read(zip, &s.href)?, saved))
            })
        })
        .collect::<Result<Vec<_>>>()?;
    if let Some((href, _)) = groups.first() {
        bail!("EPUB state refers to undeclared body resources: {href}");
    }
    let pages = if options.bilingual {
        let pages = ordered_map(inputs, |(spec, bytes, saved)| {
            prepare(spec, bytes, saved, options)
        })
        .into_iter()
        .collect::<Result<Vec<_>>>()?;
        let links = sources(&pages);
        ordered_map(pages, |page| output(page, &links, options, &finish))
    } else {
        ordered_map(inputs, |(spec, bytes, saved)| {
            output(
                prepare(spec, bytes, saved, options)?,
                &LinkMap::new(),
                options,
                &finish,
            )
        })
    };
    let mut rendered = IndexMap::new();
    let mut warnings = Vec::new();
    for page in pages {
        let (href, page, missing) = page?;
        rendered.insert(href, page);
        warnings.extend(missing);
    }
    Ok((rendered, warnings))
}

fn replace_xml_nodes(text: &str, mut edits: Vec<(ops::Range<usize>, String)>) -> String {
    if let Some(range) = legacy_xml_encoding(text) {
        edits.push((range, "utf-8".to_owned()));
    }
    edits.sort_by_key(|e| e.0.start);
    let mut result = String::with_capacity(text.len());
    let mut cursor = 0;
    for (range, patch) in edits {
        if range.start < cursor {
            continue;
        }
        result.push_str(&text[cursor..range.start]);
        result.push_str(&patch);
        cursor = range.end;
    }
    result.push_str(&text[cursor..]);
    result
}

fn xml_contents(
    text: &str,
    node: roxmltree::Node<'_, '_>,
    value: &str,
) -> (std::ops::Range<usize>, String) {
    let range = node.range();
    let mut quote = None;
    let mut start = range.start
        + text[range.clone()]
            .bytes()
            .position(|c| {
                if let Some(delimiter) = quote {
                    if c == delimiter {
                        quote = None;
                    }
                } else if matches!(c, b'\'' | b'"') {
                    quote = Some(c);
                } else if c == b'>' {
                    return true;
                }
                false
            })
            .unwrap_or(0)
        + 1;
    let end = text[range.clone()]
        .rfind("</")
        .map_or(range.end, |end| range.start + end);
    let mut escaped = String::new();
    if start == range.end && text[..start].ends_with("/>") {
        start -= 2;
        escaped.push('>');
        escape(&mut escaped, value, false);
        let qualified = text[range.start + 1..]
            .split(|ch: char| ch.is_whitespace() || matches!(ch, '/' | '>'))
            .next()
            .unwrap();
        escaped.push_str(&format!("</{qualified}>"));
    } else {
        escape(&mut escaped, value, false);
    }
    (start..end, escaped)
}

fn xml_append(
    text: &str,
    node: roxmltree::Node<'_, '_>,
    child: &str,
) -> (std::ops::Range<usize>, String) {
    let (range, patch) = xml_contents(text, node, "");
    if range.end == node.range().end {
        (range, format!(">{child}{}", &patch[1..]))
    } else {
        (range.end..range.end, child.to_owned())
    }
}

/// Whether two language tags name the same language, treating a bare tag as covering its
/// regional or script variants (`en` matches `en-US`, `zh-Hans` does not match `zh-Hant`).
fn same_language(left: &str, right: &str) -> bool {
    let normalize = |tag: &str| tag.trim().replace('_', "-").to_ascii_lowercase();
    let (left, right) = (normalize(left), normalize(right));
    let (short, long) = if left.len() <= right.len() {
        (left, right)
    } else {
        (right, left)
    };
    !short.is_empty() && (long == short || long.starts_with(&format!("{short}-")))
}

fn translate_opf_field(
    text: &str,
    node: roxmltree::Node<'_, '_>,
    value: &str,
    lang: &str,
    edits: &mut Vec<(ops::Range<usize>, String)>,
) -> bool {
    if strip(node.text().unwrap_or("")) == value {
        return false;
    }
    let mut escaped_lang = String::new();
    escape(&mut escaped_lang, lang, true);
    let attribute = format!("xml:lang=\"{escaped_lang}\"");
    const XML: &str = "http://www.w3.org/XML/1998/namespace";
    if let Some(existing) = node.attribute_node((XML, "lang")) {
        edits.push((existing.range(), attribute));
    } else {
        let range = node.range();
        let at = range.start
            + text[range]
                .find(|c: char| c.is_whitespace() || matches!(c, '/' | '>'))
                .unwrap();
        edits.push((at..at, format!(" {attribute}")));
    }
    edits.push(xml_contents(text, node, value));
    true
}

fn rewrite_xml(
    bytes: &[u8],
    href: &str,
    opf: bool,
    options: &Options,
    about: Option<&str>,
) -> Option<String> {
    let text = decode_xml(bytes);
    let doc = xml(&text).ok()?;
    let mut edits = Vec::new();
    if opf {
        const DC: &str = "http://purl.org/dc/elements/1.1/";
        let field = |name| {
            doc.descendants()
                .find(|n| n.has_tag_name((DC, name)))
                .or_else(|| doc.descendants().find(|n| n.has_tag_name(name)))
        };
        let p = options.presentation.as_ref()?;
        // Metadata nodes now carrying target-language text.
        let mut localized = HashSet::new();
        if !p.title.is_empty() {
            if let Some(n) = field("title") {
                if p.original_title.is_some() {
                    // Only a changed saved translation is target-language text; the suffixed
                    // fallback title and an unchanged fallback keep the source text.
                    if translate_opf_field(&text, n, &p.title, &p.lang, &mut edits) {
                        localized.insert(n.id());
                    }
                } else {
                    edits.push(xml_contents(&text, n, &p.title));
                }
                if let Some(orig) = p
                    .original_title
                    .as_deref()
                    .map(strip)
                    .filter(|s| !s.is_empty() && *s != p.title)
                {
                    if let Some(meta) = doc.descendants().find(|n| n.has_tag_name("metadata")) {
                        let has_source_title = meta.descendants().any(|child| {
                            child.has_tag_name("meta")
                                && child.attribute("name") == Some("wenyi:source-title")
                        });
                        if !has_source_title {
                            let qualified = text[meta.range().start + 1..]
                                .split(|c: char| c.is_whitespace() || c == '/' || c == '>')
                                .next()
                                .unwrap();
                            let prefix = qualified
                                .split_once(':')
                                .map_or(String::new(), |(p, _)| format!("{p}:"));
                            let mut escaped_orig = String::new();
                            escape(&mut escaped_orig, orig, true);
                            edits.push(xml_append(
                                &text,
                                meta,
                                &format!(
                                    "<{prefix}meta name=\"wenyi:source-title\" content=\"{escaped_orig}\"/>"
                                ),
                            ));
                        }
                    }
                }
            }
        }
        if let Some(desc) = p
            .description
            .as_deref()
            .map(strip)
            .filter(|s| !s.is_empty())
        {
            if let Some(n) = field("description") {
                if translate_opf_field(&text, n, desc, &p.lang, &mut edits) {
                    localized.insert(n.id());
                }
            }
        }
        if !p.subjects.is_empty() {
            let mut subject_nodes: Vec<_> = doc
                .descendants()
                .filter(|n| {
                    n.has_tag_name((DC, "subject")) && !strip(n.text().unwrap_or("")).is_empty()
                })
                .collect();
            if subject_nodes.is_empty() {
                subject_nodes = doc
                    .descendants()
                    .filter(|n| {
                        n.has_tag_name("subject") && !strip(n.text().unwrap_or("")).is_empty()
                    })
                    .collect();
            }
            for (node, translated) in subject_nodes.into_iter().zip(p.subjects.iter()) {
                let value = strip(translated);
                if !value.is_empty() && translate_opf_field(&text, node, value, &p.lang, &mut edits)
                {
                    localized.insert(node.id());
                }
            }
        }
        if !p.creators.is_empty() {
            let mut creator_nodes: Vec<_> = doc
                .descendants()
                .filter(|n| {
                    n.has_tag_name((DC, "creator")) && !strip(n.text().unwrap_or("")).is_empty()
                })
                .collect();
            if creator_nodes.is_empty() {
                creator_nodes = doc
                    .descendants()
                    .filter(|n| {
                        n.has_tag_name("creator") && !strip(n.text().unwrap_or("")).is_empty()
                    })
                    .collect();
            }
            for (node, translated) in creator_nodes.into_iter().zip(p.creators.iter()) {
                let value = strip(translated);
                // A name already tagged in the target language is kept as published.
                // Includes a language inherited from <metadata> or the package.
                let tagged_target = node
                    .ancestors()
                    .find_map(|a| a.attribute(("http://www.w3.org/XML/1998/namespace", "lang")))
                    .is_some_and(|lang| same_language(lang, &p.lang));
                if !value.is_empty()
                    && !tagged_target
                    && translate_opf_field(&text, node, value, &p.lang, &mut edits)
                {
                    localized.insert(node.id());
                }
            }
        }
        // The package default language would otherwise keep labelling untagged metadata
        // (such as a translated title) as the source language. Untagged metadata that stays
        // untranslated keeps the source default through an explicit attribute.
        const XML: &str = "http://www.w3.org/XML/1998/namespace";
        if let Some(lang) = doc.root_element().attribute_node((XML, "lang")) {
            if !same_language(lang.value(), &p.lang) {
                let mut escaped = String::new();
                escape(&mut escaped, &p.lang, true);
                edits.push((lang.range_value(), escaped));
                let mut source_lang = String::new();
                escape(&mut source_lang, lang.value(), true);
                let metadata = doc.descendants().find(|n| n.has_tag_name("metadata"));
                for node in metadata
                    .into_iter()
                    .flat_map(|m| m.descendants())
                    .filter(|n| {
                        let dc = n.tag_name().namespace() == Some(DC);
                        n.is_element()
                        && !(dc
                            && matches!(n.tag_name().name(), "identifier" | "language" | "date"))
                        && n.attribute("property") != Some("dcterms:modified")
                        // Dates, numbers and similar values carry no language.
                        && n.text().unwrap_or("").chars().any(char::is_alphabetic)
                        && !localized.contains(&n.id())
                        && n.attribute_node((XML, "lang")).is_none()
                        && n.ancestors()
                            .skip(1)
                            .find(|a| a.attribute_node((XML, "lang")).is_some())
                            .is_some_and(|a| a == doc.root_element())
                        && !strip(n.text().unwrap_or("")).is_empty()
                    })
                {
                    let range = node.range();
                    let at = range.start
                        + text[range]
                            .find(|c: char| c.is_whitespace() || matches!(c, '/' | '>'))
                            .unwrap();
                    edits.push((at..at, format!(" xml:lang=\"{source_lang}\"")));
                }
            }
        }
        if let Some(n) = field("language") {
            edits.push(xml_contents(&text, n, &p.lang));
        } else if let Some(n) = doc.descendants().find(|n| n.has_tag_name("metadata")) {
            let mut lang = String::new();
            escape(&mut lang, &p.lang, false);
            edits.push(xml_append(
                &text,
                n,
                &format!("<dc:language xmlns:dc=\"{DC}\">{lang}</dc:language>"),
            ));
        }
        if p.horizontal {
            for n in doc.descendants().filter(|n| n.has_tag_name("spine")) {
                if let Some(a) = n.attribute_node("page-progression-direction") {
                    edits.push((a.range_value(), "ltr".to_owned()));
                } else {
                    let at = n.range().start
                        + text[n.range()]
                            .find(|c: char| c.is_whitespace() || c == '>' || c == '/')
                            .unwrap_or(0);
                    edits.push((at..at, " page-progression-direction=\"ltr\"".to_owned()));
                }
            }
        }
        if let Some(href) = about {
            about_edits(&text, &doc, href, &mut edits);
        }
    } else {
        if let Some(title) = options
            .presentation
            .as_ref()
            .map(|p| strip(&p.title))
            .filter(|t| !t.is_empty())
        {
            if let Some(doc_title) = doc
                .descendants()
                .find(|n| n.has_tag_name("docTitle"))
                .and_then(|n| n.descendants().find(|n| n.has_tag_name("text")))
            {
                edits.push(xml_contents(&text, doc_title, title));
            }
        }
        let entries: HashMap<_, _> = options
            .toc_entries
            .iter()
            .filter(|e| e.toc_path == href)
            .map(|e| (e.node_index, e))
            .collect();
        for (i, (n, ..)) in ncx_points(&doc).into_iter().enumerate() {
            let link = n
                .children()
                .find(|n| n.has_tag_name("content"))
                .and_then(|n| n.attribute("src"))
                .unwrap_or("");
            if let Some(title) = entries.get(&i).and_then(|e| e.title(link)) {
                if let Some(label) = n
                    .children()
                    .find(|n| n.has_tag_name("navLabel"))
                    .and_then(|n| n.descendants().find(|n| n.has_tag_name("text")))
                {
                    edits.push(xml_contents(&text, label, title));
                }
            }
        }
    }
    (!edits.is_empty()).then(|| replace_xml_nodes(&text, edits))
}

pub fn about_name(number: usize) -> String {
    if number == 0 {
        "trans-novel-about.xhtml".to_owned()
    } else {
        format!("trans-novel-about-{number}.xhtml")
    }
}

fn about_edits(
    text: &str,
    doc: &roxmltree::Document<'_>,
    href: &str,
    edits: &mut Vec<(std::ops::Range<usize>, String)>,
) -> bool {
    let Some(manifest) = doc.descendants().find(|n| n.has_tag_name("manifest")) else {
        return false;
    };
    let Some(spine) = doc.descendants().find(|n| n.has_tag_name("spine")) else {
        return false;
    };
    let ids: HashSet<_> = manifest
        .descendants()
        .filter_map(|n| n.attribute("id"))
        .collect();
    let id = if !ids.contains("trans-novel-about") {
        "trans-novel-about".to_owned()
    } else {
        (1..)
            .map(|n| format!("trans-novel-about-{n}"))
            .find(|n| !ids.contains(n.as_str()))
            .unwrap()
    };
    for (node, tag, attrs) in [
        (
            manifest,
            "item",
            format!("id=\"{id}\" href=\"{href}\" media-type=\"application/xhtml+xml\""),
        ),
        (spine, "itemref", format!("idref=\"{id}\"")),
    ] {
        let qualified = text[node.range().start + 1..]
            .split(|c: char| c.is_whitespace() || c == '/' || c == '>')
            .next()
            .unwrap();
        let prefix = qualified
            .split_once(':')
            .map_or(String::new(), |(p, _)| format!("{p}:"));
        let child = format!("<{prefix}{tag} {attrs}/>");
        edits.push(xml_append(text, node, &child));
    }
    true
}

struct AboutMember {
    opf: String,
    href: String,
    path: String,
}

fn about_member(zip: &mut Archive) -> Option<AboutMember> {
    let bytes = read(zip, "META-INF/container.xml").ok()?;
    let text = decode_xml(&bytes);
    let doc = xml(&text).ok()?;
    let opf = doc
        .descendants()
        .filter(|n| n.has_tag_name("rootfile"))
        .filter_map(|n| n.attribute("full-path"))
        .map(strip)
        .find(|path| !path.is_empty())?
        .to_owned();
    let data = read(zip, &opf).ok()?;
    let data = decode_xml(&data);
    let doc = xml(&data).ok()?;
    if !doc.descendants().any(|n| n.has_tag_name("manifest"))
        || !doc.descendants().any(|n| n.has_tag_name("spine"))
    {
        return None;
    }
    let dir = opf.rsplit_once('/').map(|(d, _)| d);
    let (href, path) = (0..).find_map(|n| {
        let href = about_name(n);
        let path = dir.map_or_else(|| href.clone(), |d| format!("{d}/{href}"));
        zip.index_for_name(&path).is_none().then_some((href, path))
    })?;
    Some(AboutMember { opf, href, path })
}

pub(crate) fn append_about(source: &Path, destination: &Path, page: &str) -> Result<bool> {
    let mut zip = open(source)?;
    let Some(about) = about_member(&mut zip) else {
        return Ok(false);
    };
    rewrite(
        &mut zip,
        destination,
        |zip, href| {
            if href != about.opf {
                return Ok(None);
            }
            let bytes = read(zip, href)?;
            let text = decode_xml(&bytes);
            let doc = xml(&text)?;
            let mut edits = Vec::new();
            about_edits(&text, &doc, &about.href, &mut edits);
            Ok(Some(replace_xml_nodes(&text, edits)))
        },
        Some((&about.path, page)),
    )?;
    Ok(true)
}

fn vertical(zip: &mut Archive) -> bool {
    let mut names: Vec<_> = zip
        .file_names()
        .filter(|name| {
            let lower = name.to_ascii_lowercase();
            is_html(&lower) || lower.ends_with(".css") || lower.ends_with(".opf")
        })
        .map(str::to_owned)
        .collect();
    names.sort_by_cached_key(|n| is_html(&n.to_ascii_lowercase()));
    names
        .iter()
        .any(|name| read(zip, name).is_ok_and(|data| VERTICAL.is_match(&data)))
}

pub fn export(source: &Path, destination: &Path, mut options: Options) -> Result<Vec<String>> {
    let mut zip = open(source)?;
    if options.presentation.is_none() {
        return Err(anyhow!("Missing EPUB presentation options"));
    }
    let about = options.about.as_ref().and_then(|_| about_member(&mut zip));
    options.presentation.as_mut().unwrap().horizontal |= vertical(&mut zip);
    let (mut rendered, warnings) = resources(&mut zip, &mut options)?;
    let ncx: HashSet<_> = options
        .toc_entries
        .iter()
        .filter(|e| e.kind.as_deref() == Some("ncx"))
        .map(|e| e.toc_path.as_str())
        .collect();
    let extra = about
        .as_ref()
        .zip(options.about.as_deref())
        .map(|(about, content)| (about.path.as_str(), content));
    rewrite(
        &mut zip,
        destination,
        |zip, href| {
            let lower = href.to_ascii_lowercase();
            let is_package = lower.ends_with(".opf")
                || options.package_path.as_deref() == Some(href)
                || about.as_ref().is_some_and(|a| a.opf == href);
            let updated = if let Some(text) = rendered.shift_remove(href) {
                Some(text)
            } else if is_package || lower.ends_with(".ncx") || ncx.contains(href) {
                let extra = about
                    .as_ref()
                    .filter(|a| a.opf == href)
                    .map(|a| a.href.as_str());
                rewrite_xml(&read(zip, href)?, href, is_package, &options, extra)
            } else if is_html(&lower) {
                let data = read(zip, href)?;
                let text = decode_xhtml_for_export(&data);
                let mut dom = Dom::parse(&text);
                rewrite_nav(&mut dom, href, &options);
                present(
                    &mut dom,
                    href,
                    options.presentation.as_ref().unwrap(),
                    &options,
                )
                .then(|| dom.epub_html())
            } else {
                None
            };
            Ok(updated)
        },
        extra,
    )?;
    Ok(warnings)
}

#[cfg(test)]
mod tests {
    use super::same_language;

    #[test]
    fn same_language_keeps_script_and_region_variants_apart() {
        assert!(same_language("en", "en-US"));
        assert!(same_language("EN_us", "en-us"));
        assert!(same_language("zh", "zh-Hant"));
        assert!(!same_language("zh-Hans", "zh-Hant"));
        assert!(!same_language("en", "eng"));
        assert!(!same_language("", "en"));
    }
}
