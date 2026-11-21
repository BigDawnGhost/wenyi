//! Full-document annotation for the plain, canonical subset of standalone HTML.

use pyo3::{prelude::*, types::PyBytes};
use serde::Serialize;

use crate::epub::{
    dom::{strip, Attrs, Dom, Id, Kind},
    extract::{normalize, ANCHOR},
};

#[derive(Default, Serialize)]
struct Chapter {
    index: usize,
    title: String,
    template: String,
    segments: Vec<Segment>,
}

#[derive(Serialize)]
struct Segment {
    index: usize,
    source: String,
    kind: &'static str,
    anchor: String,
    meta: serde_json::Value,
}

#[derive(Serialize)]
struct DocumentParts {
    chapters: Vec<Chapter>,
    head_html: String,
    html_attrs: Attrs,
    body_attrs: Attrs,
}

fn heading(tag: &str) -> bool {
    matches!(tag, "h1" | "h2" | "h3" | "h4" | "h5" | "h6")
}

fn gap(dom: &Dom, id: Id) -> Option<&str> {
    match dom.kind(id) {
        Kind::Text(text) if matches!(text.as_str(), " " | "\n") => Some(text),
        _ => None,
    }
}

fn plain(dom: &Dom, id: Id) -> Option<&str> {
    match dom.children(id) {
        [] => Some(""),
        [text] => match dom.kind(*text) {
            Kind::Text(value) => Some(value),
            _ => None,
        },
        _ => None,
    }
}

fn shell_attrs(dom: &Dom, id: Id) -> Option<&Attrs> {
    match dom.kind(id) {
        Kind::Tag(_, attrs)
            if attrs
                .keys()
                .all(|key| matches!(key.as_str(), "id" | "lang" | "xml:lang" | "dir")) =>
        {
            Some(attrs)
        }
        _ => None,
    }
}

fn annotate(
    input: &str,
    chapter_tags: &[String],
    rebuild_shell: bool,
) -> Option<(DocumentParts, String)> {
    // Separate the declaration so its serializer newline does not participate
    // in checking whether the explicit document shell is canonical.
    let (declaration, markup) = input
        .strip_prefix("<!DOCTYPE html>")
        .map_or(("", input), |rest| ("<!DOCTYPE html>\n", rest));
    if !markup.trim_start_matches([' ', '\n']).starts_with("<html") || markup.contains(['\0', '\r'])
    {
        return None;
    }
    let mut dom = Dom::parse(markup);
    let root = dom.direct(0, "html")?;
    if dom
        .children(0)
        .iter()
        .any(|&id| id != root && gap(&dom, id).is_none())
    {
        return None;
    }
    let head = dom.direct(root, "head")?;
    let body = dom.direct(root, "body")?;
    let html_attrs = shell_attrs(&dom, root)?.clone();
    let body_attrs = shell_attrs(&dom, body)?.clone();
    shell_attrs(&dom, head)?;

    // Refuse constructs whose text, metadata or repaired tree might differ
    // from html.parser. In particular there are no protected/inline elements,
    // translated attributes, temporary markers, or loose body prose here.
    for &node in dom.children(head) {
        if gap(&dom, node).is_some() {
            continue;
        }
        match dom.kind(node) {
            Kind::Tag(name, attrs) if name == "title" && attrs.is_empty() => {
                plain(&dom, node)?;
            }
            Kind::Tag(name, attrs)
                if name == "meta"
                    && attrs.len() == 1
                    && dom.attr(node, "charset") == Some("utf-8")
                    && dom.children(node).is_empty() => {}
            _ => return None,
        }
    }
    for &node in dom.children(body) {
        if gap(&dom, node).is_some() {
            continue;
        }
        if dom.name(node) != "p" && !heading(dom.name(node)) {
            return None;
        }
        shell_attrs(&dom, node)?;
        plain(&dom, node)?;
    }
    // Reject spellings the two parsers may normalize differently (e.g.
    // omitted closing tags, mixed-case names, and noncanonical entities).
    if dom.html(0) != markup {
        return None;
    }

    // Match html_document_soup: whitespace before the explicit body goes at
    // the end of the head. Trailing whitespace goes into the body and, unlike
    // the head, is collapsed again when the legacy chapter fragment is parsed.
    let mut seen = 0;
    for node in dom.children(root).to_vec() {
        if (node == head && seen == 0) || (node == body && seen == 1) {
            seen += 1;
        } else if seen < 2 {
            gap(&dom, node)?;
            dom.append(head, node);
        } else {
            let trailing = gap(&dom, node)?;
            let last = dom.children(body).last().copied();
            if let Some((last, previous)) = last.and_then(|id| Some((id, gap(&dom, id)?))) {
                let combined = if previous == "\n" || trailing == "\n" {
                    "\n"
                } else {
                    " "
                };
                dom.detach(last);
                dom.detach(node);
                dom.append_text(body, combined);
            } else {
                dom.append(body, node);
            }
        }
    }
    if seen != 2 {
        return None;
    }
    let head_html = dom.inner_html(head);

    let nodes = dom.children(body).to_vec();
    let mut starts = vec![0];
    let mut previous_boundary = false;
    for (position, &node) in nodes.iter().enumerate() {
        if gap(&dom, node).is_none() {
            let boundary = chapter_tags.iter().any(|tag| tag == dom.name(node));
            if position > 0 && boundary && !previous_boundary {
                starts.push(position);
            }
            previous_boundary = boundary;
        }
    }
    starts.push(nodes.len());

    let mut chapters = Vec::new();
    let mut pending = String::new();
    for bounds in starts.windows(2) {
        let interval = &nodes[bounds[0]..bounds[1]];
        let index = chapters.len();
        let title = if interval.first().is_some_and(|&id| heading(dom.name(id))) {
            interval
                .iter()
                .copied()
                .filter(|&id| gap(&dom, id).is_none())
                .take_while(|&id| heading(dom.name(id)))
                .map(|id| {
                    plain(&dom, id)
                        .unwrap()
                        .split(|c: char| c.is_whitespace() || matches!(c, '\x1c'..='\x1f'))
                        .filter(|part| !part.is_empty())
                        .collect::<Vec<_>>()
                        .join(" ")
                })
                .filter(|part| !part.is_empty())
                .collect::<Vec<_>>()
                .join(" / ")
        } else {
            String::new()
        };
        let mut segments = Vec::new();
        for &node in interval {
            if gap(&dom, node).is_none() {
                let (source, _) = normalize(plain(&dom, node)?, &[]);
                if !source.is_empty() {
                    let kind = if heading(dom.name(node)) {
                        "heading"
                    } else {
                        "text"
                    };
                    let ordinal = segments.len();
                    let anchor = format!("tn{index}_{ordinal}");
                    dom.set(node, ANCHOR, &anchor);
                    segments.push(Segment {
                        index: ordinal,
                        source,
                        kind,
                        anchor,
                        meta: serde_json::json!({"html_markup": {"version": 1}}),
                    });
                }
            }
            pending.push_str(&dom.html(node));
        }
        if !segments.is_empty() {
            let title = if title.is_empty() {
                segments
                    .iter()
                    .find(|segment| segment.kind == "heading")
                    .map_or_else(String::new, |segment| segment.source.clone())
            } else {
                title
            };
            chapters.push(Chapter {
                index,
                title,
                template: std::mem::take(&mut pending),
                segments,
            });
        }
    }
    if !strip(&pending).is_empty() {
        if let Some(last) = chapters.last_mut() {
            last.template.push_str(&pending);
        } else {
            chapters.push(Chapter {
                template: pending,
                ..Chapter::default()
            });
        }
    }
    let template = if rebuild_shell {
        format!("{declaration}{}", dom.html(0))
    } else {
        String::new()
    };
    Some((
        DocumentParts {
            chapters,
            head_html,
            html_attrs,
            body_attrs,
        },
        template,
    ))
}

#[pyfunction]
fn html_annotate_document<'py>(
    py: Python<'py>,
    html: &str,
    chapter_tags: Vec<String>,
    rebuild_shell: bool,
) -> PyResult<Option<(Bound<'py, PyBytes>, String)>> {
    py.detach(|| {
        annotate(html, &chapter_tags, rebuild_shell)
            .map(|(parts, template)| serde_json::to_vec(&parts).map(|data| (data, template)))
            .transpose()
    })
    .map(|result| result.map(|(data, template)| (PyBytes::new(py, &data), template)))
    .map_err(|error| crate::value_error(error.into()))
}

pub(super) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(html_annotate_document, m)?)
}
