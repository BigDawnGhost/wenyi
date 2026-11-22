//! Standalone HTML planning and conservative native markup transformations.
//!
//! Python's tolerant HTML tree is still authoritative. Only canonical trees
//! qualify for native serialization. Paired and rich backfill have additional
//! compatibility checks in `paired`.

mod ingest;

use std::collections::HashMap;

use base64::{engine::general_purpose::STANDARD, Engine};
use pyo3::prelude::*;

use crate::epub::{
    dom::{Dom, Id, Kind},
    extract::{normalize, ANCHOR, ANNOTATION, INLINE, LINE},
    html::srcset,
};

mod paired;

const ATTRIBUTE: &str = "data-tn-attribute-id";

// Element policy stays with the tolerant parser: boundary, enter, ruby
// exception for parent's barrier computation.
type RegionNode = (Id, bool, bool, bool);
type Region = (Id, bool, Vec<Id>);

#[pyfunction]
fn html_regions(py: Python<'_>, nodes: Vec<RegionNode>) -> Vec<Region> {
    py.detach(|| {
        let mut children = vec![Vec::new(); nodes.len() + 1];
        let mut barrier = vec![false; nodes.len() + 1];
        for (offset, &(parent, _, _, _)) in nodes.iter().enumerate() {
            let node = offset + 1;
            if parent >= node {
                return vec![];
            }
            children[parent].push(node);
        }
        for (offset, &(parent, boundary, _, ruby_exception)) in nodes.iter().enumerate().rev() {
            if !ruby_exception && (boundary || barrier[offset + 1]) {
                barrier[parent] = true;
            }
        }
        let mut regions = Vec::new();
        let mut stack = vec![(0, 0)];
        while let Some((parent, pos)) = stack.pop() {
            let siblings = &children[parent];
            let mut end = pos;
            while let Some(&node) = siblings.get(end) {
                let (_, boundary, _, _) = nodes[node - 1];
                if boundary || barrier[node] {
                    break;
                }
                end += 1;
            }
            if end != pos {
                let whole_parent = parent != 0 && pos == 0 && end == siblings.len();
                regions.push((parent, whole_parent, siblings[pos..end].to_vec()));
            }
            if let Some(&node) = siblings.get(end) {
                stack.push((parent, end + 1));
                if nodes[node - 1].2 {
                    stack.push((node, 0));
                }
            }
        }
        regions
    })
}

#[pyfunction]
fn html_normalize_text(py: Python<'_>, raw: &str, offsets: Vec<isize>) -> (String, Vec<usize>) {
    py.detach(|| {
        if offsets.is_empty() {
            return normalize(raw, &[]);
        }
        // Python metadata uses Unicode scalar indices; the shared EPUB engine's
        // input offsets are UTF-8 bytes, but it returns scalar indices.
        let boundaries: Vec<_> = raw
            .char_indices()
            .map(|(offset, _)| offset)
            .chain(std::iter::once(raw.len()))
            .collect();
        let offsets: Vec<_> = offsets
            .into_iter()
            .map(|offset| boundaries[(offset.max(0) as usize).min(boundaries.len() - 1)])
            .collect();
        normalize(raw, &offsets)
    })
}

fn canonical(input: &str) -> Option<Dom> {
    // The arena does not model HTMLParser's specialized text classes or
    // arbitrary declaration kinds. Gate native mutations separately as well.
    if input.contains(['\0', '\r']) {
        return None;
    }
    let dom = Dom::parse(input);
    let rendered = dom.html(0);
    // Both serializers add a newline after a doctype. The old Python stages
    // reparsed the string, so keep that additional line when it appears here.
    let doctype_only = input
        .strip_prefix("<!DOCTYPE html>\n")
        .is_some_and(|body| rendered.strip_prefix("<!DOCTYPE html>\n\n") == Some(body));
    (rendered == input || doctype_only).then_some(dom)
}

type PlainEdit = (String, String, String, Option<(String, String)>, bool);

fn translatable_attr(dom: &Dom, node: Id, attr: &str) -> bool {
    matches!(
        attr,
        "abbr"
            | "alt"
            | "aria-description"
            | "aria-label"
            | "aria-placeholder"
            | "aria-roledescription"
            | "aria-valuetext"
            | "label"
            | "placeholder"
            | "title"
    ) || (attr == "value"
        && dom.name(node) == "input"
        && dom.attr(node, "type").is_some_and(|value| {
            ["button", "reset", "submit"]
                .iter()
                .any(|kind| value.eq_ignore_ascii_case(kind))
        }))
}

#[pyfunction]
fn html_backfill_plain(py: Python<'_>, template: &str, edits: Vec<PlainEdit>) -> Option<String> {
    py.detach(|| {
        let mut dom = canonical(template)?;
        let nodes: Vec<_> = dom.tags(0).collect();
        let mut anchors = HashMap::new();
        let mut attribute_nodes = HashMap::new();
        for &id in &nodes {
            if dom.attr(id, ANNOTATION).is_some() || dom.attr(id, INLINE).is_some() {
                return None;
            }
            if let Some(anchor) = dom.attr(id, ANCHOR) {
                anchors.insert(anchor.to_owned(), id);
            }
            if let Some(slot) = dom.attr(id, ATTRIBUTE) {
                attribute_nodes.insert(slot.to_owned(), id);
            }
        }
        for (anchor, target, source, slot, html_markup) in edits {
            if let Some((slot, attr)) = slot {
                let id = *attribute_nodes.get(&slot)?;
                if !translatable_attr(&dom, id, &attr) {
                    return None;
                }
                dom.attr(id, &attr)?;
                dom.set(id, &attr, target);
                continue;
            }
            let id = *anchors.get(&anchor)?;
            if target != source {
                if !dom
                    .children(id)
                    .iter()
                    .all(|&child| matches!(dom.kind(child), Kind::Text(_)))
                    || matches!(
                        dom.name(id),
                        "script" | "style" | "template" | "textarea" | "xmp"
                    )
                {
                    return None;
                }
                let original = dom.text(id, "", false);
                let wrapped = html_markup && dom.attr(id, LINE).is_some();
                let is_space = |c: char| c.is_whitespace() || matches!(c, '\x1c'..='\x1f');
                dom.clear(id);
                if wrapped {
                    let edge = original.len() - original.trim_start_matches(is_space).len();
                    dom.append_text(id, &original[..edge]);
                }
                crate::epub::export::breaks(&mut dom, id, &target);
                if wrapped {
                    let edge = original.trim_end_matches(is_space).len();
                    dom.append_text(id, &original[edge..]);
                }
            }
            dom.attrs_mut(id).remove(ANCHOR);
        }
        for id in nodes {
            dom.attrs_mut(id).remove(ATTRIBUTE);
            if dom.attr(id, LINE).is_some() {
                dom.unwrap(id);
            }
        }
        Some(dom.html(0))
    })
}

#[pyfunction]
fn html_present(
    py: Python<'_>,
    html: &str,
    language: &str,
    style_id: &str,
    css: &str,
) -> Option<String> {
    py.detach(|| {
        let mut dom = canonical(html)?;
        let root = dom.direct(0, "html")?;
        let head = dom.direct(root, "head")?;
        let body = dom.direct(root, "body")?;
        // Optional shells and loose siblings still need html_document_soup's
        // exact recovery policy for choosing head/body contents.
        if dom.children(root) != [head, body] {
            return None;
        }
        let metas: Vec<_> = dom
            .tags(head)
            .filter(|&id| {
                dom.name(id) == "meta"
                    && (dom.attr(id, "charset").is_some()
                        || dom
                            .attr(id, "http-equiv")
                            .is_some_and(|v| v.eq_ignore_ascii_case("content-type")))
            })
            .collect();
        for id in metas {
            dom.detach(id);
        }
        let charset = dom.tag("meta");
        dom.set(charset, "charset", "utf-8");
        dom.prepend(head, charset);
        if !css.is_empty() && !dom.tags(0).any(|id| dom.attr(id, "id") == Some(style_id)) {
            let style = dom.tag("style");
            dom.set(style, "id", style_id);
            dom.append_text(style, css);
            dom.append(head, style);
        }
        dom.set(root, "lang", language);
        if dom.attr(root, "xml:lang").is_some() {
            dom.set(root, "xml:lang", language);
        }
        Some(dom.html(0))
    })
}

type MediaEdit = (Id, &'static str, Vec<(String, String)>);

fn media_plan(dom: &Dom) -> Vec<MediaEdit> {
    let mut edits = Vec::new();
    for id in dom.tags(0) {
        let attrs: &[&str] = match dom.name(id) {
            "img" | "source" | "embed" | "audio" => &["src"],
            "object" => &["data"],
            "video" => &["src", "poster"],
            "image" => &["href", "xlink:href"],
            _ => continue,
        };
        for &attr in attrs {
            if let Some(value) = dom.attr(id, attr) {
                edits.push((id, attr, vec![(value.to_owned(), String::new())]));
            }
        }
        if let Some(value) = dom.attr(id, "srcset") {
            edits.push((
                id,
                "srcset",
                srcset(value)
                    .map(|(url, descriptor)| (url.to_owned(), descriptor.to_owned()))
                    .collect(),
            ));
        }
    }
    edits
}

#[pyfunction]
fn html_inline_media(
    py: Python<'_>,
    html: &str,
    load: &Bound<'_, PyAny>,
) -> PyResult<Option<String>> {
    let Some((mut dom, edits)) = py.detach(|| {
        let dom = canonical(html)?;
        let plan = media_plan(&dom);
        Some((dom, plan))
    }) else {
        return Ok(None);
    };
    let mut urls = HashMap::new();
    for (_, _, candidates) in &edits {
        for (url, _) in candidates {
            if !urls.contains_key(url) {
                let payload: Option<(String, Vec<u8>)> = load.call1((url,))?.extract()?;
                let data_url = payload.map(|(mime, bytes)| {
                    py.detach(|| format!("data:{mime};base64,{}", STANDARD.encode(bytes)))
                });
                urls.insert(url.clone(), data_url);
            }
        }
    }
    Ok(Some(py.detach(|| {
        for (id, attr, candidates) in edits {
            let value = candidates
                .into_iter()
                .map(|(reference, descriptor)| {
                    let url = urls
                        .get(&reference)
                        .and_then(Option::as_deref)
                        .unwrap_or(&reference);
                    if attr != "srcset" {
                        url.to_owned()
                    } else {
                        let candidate = format!("{url} {descriptor}");
                        candidate
                            .trim_end_matches(|c: char| {
                                c.is_whitespace() || matches!(c, '\x1c'..='\x1f')
                            })
                            .to_owned()
                    }
                })
                .collect::<Vec<_>>()
                .join(", ");
            dom.set(id, attr, value);
        }
        dom.html(0)
    })))
}

pub(crate) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    ingest::register(m)?;
    m.add_function(wrap_pyfunction!(html_regions, m)?)?;
    m.add_function(wrap_pyfunction!(html_normalize_text, m)?)?;
    m.add_function(wrap_pyfunction!(html_backfill_plain, m)?)?;
    m.add_function(wrap_pyfunction!(paired::html_backfill_paired, m)?)?;
    m.add_function(wrap_pyfunction!(html_present, m)?)?;
    m.add_function(wrap_pyfunction!(html_inline_media, m)?)?;
    Ok(())
}
