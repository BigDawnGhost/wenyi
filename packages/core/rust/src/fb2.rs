//! FictionBook chapters, stable segment IDs and embedded resource decoding.

use anyhow::Result;
use roxmltree::{Document, Node};
use serde::Serialize;
use serde_json::{json, Value};

fn strip(s: &str) -> &str {
    s.trim_matches(|c: char| c.is_whitespace() || matches!(c, '\x1c'..='\x1f'))
}

pub(crate) fn parse(content: &str) -> Result<Document<'_>> {
    Ok(Document::parse_with_options(
        content,
        roxmltree::ParsingOptions {
            allow_dtd: true,
            nodes_limit: 2_000_000,
            ..Default::default()
        },
    )?)
}

pub(crate) fn title(root: Node<'_, '_>, fallback: &str) -> String {
    root.descendants()
        .filter(|n| n.has_tag_name("title-info"))
        .filter_map(|n| n.children().find(|c| c.has_tag_name("book-title")))
        .map(initial_text)
        .filter(|s| !s.is_empty())
        .last()
        .map_or_else(|| fallback.to_owned(), |s| strip(&s).to_owned())
}

fn initial_text(node: Node<'_, '_>) -> String {
    // ElementTree ignores comments and processing instructions in element.text.
    node.children()
        .take_while(|n| !n.is_element())
        .filter(Node::is_text)
        .filter_map(|n| n.text())
        .collect()
}

fn text(node: Node<'_, '_>) -> String {
    node.descendants()
        .filter(Node::is_text)
        .filter_map(|n| n.text())
        .collect()
}

fn image_id<'a>(node: Node<'a, '_>) -> &'a str {
    node.attributes()
        .find(|a| a.name() == "href")
        .map_or("", |a| {
            strip(a.value().strip_prefix('#').unwrap_or(a.value()))
        })
}

fn media<'a>(node: Node<'a, '_>) -> &'a str {
    node.attribute("content-type")
        .unwrap_or("application/octet-stream")
}

#[derive(Serialize)]
struct Segment {
    index: usize,
    source: String,
    kind: &'static str,
    anchor: String,
}

#[derive(Serialize)]
struct Chapter {
    index: usize,
    title: String,
    segments: Vec<Segment>,
    meta: Value,
}

fn add(out: &mut Vec<Segment>, chapter: usize, value: &str, kind: &'static str) {
    let value = strip(value);
    if !value.is_empty() {
        let index = out.len();
        out.push(Segment {
            index,
            source: value.to_owned(),
            kind,
            anchor: format!("tn{chapter}_{index}"),
        });
    }
}

fn direct_section(section: Node<'_, '_>, index: usize) -> (String, Vec<Segment>, Vec<Value>) {
    let mut segments = Vec::new();
    let mut images = Vec::new();
    let mut title = String::new();
    for child in section.children().filter(Node::is_element) {
        if child.has_tag_name("title") {
            title = strip(&text(child)).to_owned();
            add(&mut segments, index, &title, "heading");
            continue;
        }
        let mut tasks = vec![child];
        while let Some(node) = tasks.pop() {
            let mut record_image = |image, position| {
                let id = image_id(image);
                if !id.is_empty() {
                    images.push(json!({"id": id, "position": position}));
                }
            };
            match node.tag_name().name() {
                "image" => record_image(node, segments.len()),
                "subtitle" => add(&mut segments, index, &text(node), "heading"),
                "p" | "v" | "text-author" => {
                    for image in node.descendants().filter(|n| n.has_tag_name("image")) {
                        record_image(image, segments.len());
                    }
                    add(&mut segments, index, &text(node), "text");
                }
                "epigraph" | "cite" | "poem" | "stanza" | "title" | "annotation" => {
                    tasks.extend(node.children().rev().filter(Node::is_element));
                }
                _ => {}
            }
        }
    }
    (title, segments, images)
}

fn chapters(root: Node<'_, '_>, fallback: &str) -> Vec<Chapter> {
    let mut chapters = Vec::new();
    if let Some(body) = root
        .children()
        .find(|n| n.has_tag_name("body") && n.attribute("name").unwrap_or("").is_empty())
    {
        if let Some(title) = body.children().find(|n| n.has_tag_name("title")) {
            let mut segs = Vec::new();
            for line in title.children().filter(|n| n.has_tag_name("p")) {
                add(&mut segs, 0, &text(line), "heading");
            }
            if let Some(last) = segs.last() {
                chapters.push(Chapter {
                    index: 0,
                    title: last.source.clone(),
                    segments: segs,
                    meta: json!({}),
                });
            }
        }
        let mut stack: Vec<_> = body
            .children()
            .rev()
            .filter(|n| n.has_tag_name("section"))
            .collect();
        while let Some(section) = stack.pop() {
            let index = chapters.len();
            let (mut title, segments, images) = direct_section(section, index);
            let has_children = section.children().any(|n| n.has_tag_name("section"));
            if if has_children {
                !title.is_empty() || segments.iter().any(|s| s.kind == "text")
            } else {
                !segments.is_empty()
            } {
                if title.is_empty() {
                    title = segments.first().map_or_else(
                        || format!("Chapter {}", index + 1),
                        |s| s.source.chars().take(80).collect(),
                    );
                }
                let meta = if images.is_empty() {
                    json!({})
                } else {
                    json!({"fb2_images": images})
                };
                chapters.push(Chapter {
                    index,
                    title,
                    segments,
                    meta,
                });
            }
            stack.extend(
                section
                    .children()
                    .rev()
                    .filter(|n| n.has_tag_name("section")),
            );
        }
    }
    if chapters.is_empty() {
        let mut segments = Vec::new();
        for paragraph in root.descendants().filter(|n| n.has_tag_name("p")) {
            add(&mut segments, 0, &text(paragraph), "text");
        }
        if !segments.is_empty() {
            chapters.push(Chapter {
                index: 0,
                title: fallback.to_owned(),
                segments,
                meta: json!({}),
            });
        }
    }
    chapters
}

pub(crate) fn ingest(
    content: &str,
    fallback: &str,
    path: &str,
    source_lang: &str,
    target_lang: &str,
) -> Result<Vec<u8>> {
    let doc = parse(content)?;
    let root = doc.root_element();
    let title = title(root, fallback);
    let resources: Vec<_> = root
        .descendants()
        .filter(|n| n.has_tag_name("binary"))
        .filter_map(|n| {
            let id = strip(n.attribute("id")?);
            (!id.is_empty()).then(|| json!({"id": id, "content_type": media(n)}))
        })
        .collect();
    let cover = root
        .descendants()
        .find(|n| n.has_tag_name("coverpage"))
        .and_then(|n| n.descendants().find(|c| c.has_tag_name("image")))
        .map(image_id)
        .filter(|s| !s.is_empty());
    let mut meta = json!({});
    if !resources.is_empty() {
        meta["fb2_resources"] = json!(resources);
    }
    if let Some(cover) = cover {
        meta["fb2_cover_image"] = json!(cover);
    }
    Ok(serde_json::to_vec(&json!({
        "title": title, "source_lang": source_lang, "target_lang": target_lang, "source_path": path,
        "fmt": "fb2", "chapters": chapters(root, &title), "meta": meta
    }))?)
}

pub(crate) fn binaries(content: &str) -> Result<Vec<(String, String, String)>> {
    let doc = parse(content)?;
    let mut out = Vec::new();
    for node in doc.descendants().filter(|n| n.has_tag_name("binary")) {
        let id = strip(node.attribute("id").unwrap_or(""));
        if id.is_empty() {
            continue;
        }
        let data = initial_text(node);
        if !strip(&data).is_empty() {
            out.push((id.to_owned(), media(node).to_owned(), data));
        }
    }
    Ok(out)
}
