//! ZIP package discovery, TOC identities, note contexts and logical chapter layout.

use std::{
    collections::{BTreeMap, HashMap, HashSet},
    path::Path,
};

use anyhow::{anyhow, Context, Result};
use indexmap::IndexMap;
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};

use super::{
    archive::{open, read, Archive},
    dom::{decode, decode_xml, strip, Dom, Id},
    extract::{
        annotate_with_schema, FragmentIndex, Resource, Segment, ANCHOR, ANNOTATION, EPUB_SCHEMA,
        INLINE, OBJECT,
    },
    links::{is_html, resolve, Link},
    workers::ordered_map,
};

#[derive(Clone, Deserialize, Serialize)]
pub struct Spec {
    pub index: usize,
    pub href: String,
}

pub struct Package {
    pub opf: String,
    pub title: String,
    pub description: String,
    pub subjects: Vec<String>,
    pub creators: Vec<String>,
    pub spine: Vec<Spec>,
    pub manifest: Vec<String>,
    pub toc_paths: Vec<String>,
    pub split_nested_toc: bool,
}

// Opt-in for generated source editions; ordinary EPUBs keep their existing
// chapter identities even when their publisher uses a deeply nested TOC.
pub(super) const BOUNDARY_POLICY: &str = "wenyi:chapter-boundaries";
pub(super) const ALL_TOC_HEADINGS: &str = "all-toc-headings";

pub fn xml(data: &str) -> Result<roxmltree::Document<'_>> {
    // Allow legacy DOCTYPE declarations; roxmltree never loads external entities.
    Ok(roxmltree::Document::parse_with_options(
        data,
        roxmltree::ParsingOptions {
            allow_dtd: true,
            nodes_limit: 2_000_000,
            ..Default::default()
        },
    )?)
}

fn insert_unique(output: &mut Vec<String>, seen: &mut HashSet<String>, path: String) {
    if !path.is_empty() && seen.insert(path.clone()) {
        output.push(path);
    }
}

pub fn package(zip: &mut Archive) -> Result<Package> {
    let bytes = read(zip, "META-INF/container.xml")?;
    let text = decode_xml(&bytes);
    let root = xml(&text).context("Invalid EPUB container.xml")?;
    let opf = root
        .descendants()
        .filter(|n| n.has_tag_name("rootfile"))
        .filter_map(|n| n.attribute("full-path"))
        .map(strip)
        .find(|s| !s.is_empty())
        .ok_or_else(|| anyhow!("Invalid EPUB: container.xml has no valid rootfile full-path"))?
        .to_owned();
    let bytes = read(zip, &opf)?;
    let text = decode_xml(&bytes);
    let root = xml(&text).context("Invalid EPUB package document")?;
    let mut title = String::new();
    let mut description = String::new();
    let mut subjects = Vec::new();
    // Export prefers Dublin Core creators, so positions must come from the same set.
    let (mut dc_creators, mut other_creators) = (Vec::new(), Vec::new());
    let mut split_nested_toc = false;
    let mut items = IndexMap::new();
    let (mut spine_ids, mut toc_ids, mut manifest) = (Vec::new(), Vec::new(), Vec::new());
    let mut seen = HashSet::new();
    for n in root.descendants().filter(|n| n.is_element()) {
        match n.tag_name().name() {
            "meta"
                if n.attribute("property") == Some(BOUNDARY_POLICY)
                    && n.text() == Some(ALL_TOC_HEADINGS) =>
            {
                split_nested_toc = true;
            }
            "title" if title.is_empty() => title = strip(n.text().unwrap_or("")).to_owned(),
            "description" if description.is_empty() => {
                description = strip(n.text().unwrap_or("")).to_owned();
            }
            "subject" => {
                let text = strip(n.text().unwrap_or(""));
                if !text.is_empty() {
                    subjects.push(text.to_owned());
                }
            }
            "creator" => {
                let text = strip(n.text().unwrap_or(""));
                if !text.is_empty() {
                    if n.tag_name().namespace() == Some("http://purl.org/dc/elements/1.1/") {
                        dc_creators.push(text.to_owned());
                    } else {
                        other_creators.push(text.to_owned());
                    }
                }
            }
            "item" => {
                let raw = n.attribute("href").unwrap_or("");
                let media = n.attribute("media-type").unwrap_or("");
                if media.to_ascii_lowercase().contains("html")
                    || is_html(
                        &raw.split(['?', '#'])
                            .next()
                            .unwrap_or("")
                            .trim()
                            .to_ascii_lowercase(),
                    )
                {
                    insert_unique(&mut manifest, &mut seen, resolve(&opf, strip(raw)).path);
                }
                if let Some(id) = n.attribute("id").map(strip).filter(|s| !s.is_empty()) {
                    items.insert(id, (raw, media, n.attribute("properties").unwrap_or("")));
                }
            }
            "itemref" => {
                if let Some(id) = n.attribute("idref").map(strip).filter(|s| !s.is_empty()) {
                    spine_ids.push(id);
                }
            }
            "spine" => {
                if let Some(id) = n.attribute("toc").filter(|s| !s.is_empty()) {
                    toc_ids.push(id);
                }
            }
            _ => {}
        }
    }
    let mut hrefs = Vec::new();
    seen.clear();
    for id in spine_ids {
        if let Some(&(href, media, _)) = items.get(id) {
            if media.contains("html") || is_html(href) {
                insert_unique(&mut hrefs, &mut seen, resolve(&opf, href).path);
            }
        }
    }
    let spine = hrefs
        .into_iter()
        .enumerate()
        .map(|(index, href)| Spec { index, href })
        .collect();
    let nav = items
        .iter()
        .filter(|(_, v)| v.2.split_whitespace().any(|s| s == "nav"))
        .map(|(&k, _)| k);
    let ncx = items
        .iter()
        .filter(|(_, v)| v.1 == "application/x-dtbncx+xml")
        .map(|(&k, _)| k);
    let mut toc_paths = Vec::new();
    seen.clear();
    for id in nav.chain(toc_ids).chain(ncx) {
        if let Some(&(href, _, _)) = items.get(id) {
            insert_unique(&mut toc_paths, &mut seen, resolve(&opf, href).path);
        }
    }
    Ok(Package {
        opf,
        title,
        description,
        subjects,
        creators: if dc_creators.is_empty() {
            other_creators
        } else {
            dc_creators
        },
        spine,
        manifest,
        toc_paths,
        split_nested_toc,
    })
}

pub fn book_title(path: &Path, package: &Package) -> String {
    if !package.title.is_empty() {
        return package.title.clone();
    }
    path.file_stem()
        .unwrap_or_default()
        .to_string_lossy()
        .into_owned()
}

#[derive(Debug, Deserialize, Serialize)]
pub struct Toc {
    pub entry_id: String,
    pub toc_path: String,
    pub node_index: usize,
    pub node_id: String,
    pub parent_index: Option<usize>,
    pub depth: usize,
    pub kind: String,
    pub title: String,
    pub raw_href: String,
    pub resource_href: String,
    pub fragment: String,
    pub target_key: String,
    pub external: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub segment_anchor: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub boundary_position: Option<usize>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub inherited_boundary_from: Option<String>,
}

impl Toc {
    fn new(
        path: &str,
        idx: usize,
        id: &str,
        parent: Option<usize>,
        depth: usize,
        kind: &str,
        title: String,
        href: &str,
    ) -> Self {
        let link = if href.is_empty() {
            Link {
                path: String::new(),
                fragment: String::new(),
                external: false,
            }
        } else {
            resolve(path, href)
        };
        let key = link.key();
        Self {
            entry_id: format!("{path}:{idx}"),
            toc_path: path.to_owned(),
            node_index: idx,
            node_id: id.to_owned(),
            parent_index: parent,
            depth,
            kind: kind.to_owned(),
            title,
            raw_href: href.to_owned(),
            resource_href: link.path,
            fragment: link.fragment,
            target_key: key,
            external: link.external,
            segment_anchor: None,
            boundary_position: None,
            inherited_boundary_from: None,
        }
    }
}

fn toc_navs(dom: &Dom) -> Vec<Id> {
    dom.tags(0)
        .filter(|&id| dom.name(id) == "nav")
        .filter(|&id| {
            dom.attr(id, "epub:type")
                .filter(|s| !s.is_empty())
                .or(dom.attr(id, "type"))
                .is_some_and(|s| s.split_whitespace().any(|s| s == "toc"))
        })
        .collect()
}

/// The document `<title>` and the headings of a navigation document, such as "Contents".
///
/// Headings inside a list are entry labels (returned by [`nav_labels`]) and are excluded; a
/// heading may sit inside the toc nav, in a wrapper, or just before it.
pub fn nav_headings(dom: &Dom) -> Vec<Id> {
    let mut nodes: Vec<Id> = dom
        .find(0, "head")
        .and_then(|head| dom.direct(head, "title"))
        .into_iter()
        .collect();
    let heading = |n: Id| matches!(dom.name(n), "h1" | "h2" | "h3" | "h4" | "h5" | "h6");
    let body = dom.find(0, "body").unwrap_or(0);
    nodes.extend(dom.tags(body).filter(|&n| {
        heading(n)
            && !dom
                .parents(n)
                .any(|a| heading(a) || matches!(dom.name(a), "ol" | "ul" | "li"))
    }));
    nodes
}

pub fn nav_labels(dom: &Dom) -> Vec<(Id, Id, usize, Option<usize>)> {
    let mut scopes = toc_navs(dom);
    if scopes.is_empty() {
        scopes.push(dom.tags(0).find(|&id| dom.name(id) == "nav").unwrap_or(0));
    }
    let mut labels = Vec::new();
    for scope in scopes {
        let Some(list) = dom.direct(scope, "ol").or_else(|| dom.find(scope, "ol")) else {
            continue;
        };
        let mut stack = Vec::new();
        stack.extend(
            dom.children(list)
                .iter()
                .rev()
                .filter(|&&n| dom.name(n) == "li")
                .map(|&n| (n, 0, None)),
        );
        while let Some((li, depth, mut parent)) = stack.pop() {
            if let Some(label) = dom.direct(li, "a").or_else(|| dom.direct(li, "span")) {
                let idx = labels.len();
                labels.push((li, label, depth, parent));
                parent = Some(idx);
            }
            if let Some(list) = dom.direct(li, "ol") {
                stack.extend(
                    dom.children(list)
                        .iter()
                        .rev()
                        .filter(|&&n| dom.name(n) == "li")
                        .map(|&n| (n, depth + 1, parent)),
                );
            }
        }
    }
    labels
}

pub fn ncx_points<'a, 'input>(
    doc: &'a roxmltree::Document<'input>,
) -> Vec<(roxmltree::Node<'a, 'input>, usize, Option<usize>)> {
    let mut points = Vec::new();
    let Some(map) = doc.descendants().find(|n| n.has_tag_name("navMap")) else {
        return points;
    };
    let mut stack: Vec<_> = map
        .children()
        .rev()
        .filter(|n| n.has_tag_name("navPoint"))
        .map(|n| (n, 0, None))
        .collect();
    while let Some((node, depth, parent)) = stack.pop() {
        let idx = points.len();
        points.push((node, depth, parent));
        stack.extend(
            node.children()
                .rev()
                .filter(|n| n.has_tag_name("navPoint"))
                .map(|n| (n, depth + 1, Some(idx))),
        );
    }
    points
}

fn toc_entries(
    zip: &mut Archive,
    paths: &[String],
    nav_titles: &mut Vec<String>,
) -> Result<Vec<Toc>> {
    let mut out = Vec::new();
    for path in paths {
        if zip.index_for_name(path).is_none() {
            continue;
        }
        let bytes = read(zip, path)?;
        let lower = path.to_ascii_lowercase();
        let html = is_html(&lower);
        let text = if html {
            decode(&bytes)
        } else {
            decode_xml(&bytes)
        };
        let doc = if lower.ends_with(".ncx") || text.contains("navMap") || text.contains("<ncx") {
            xml(&text).ok()
        } else {
            None
        };
        let ncx = lower.ends_with(".ncx")
            || doc.as_ref().is_some_and(|doc| {
                doc.root_element()
                    .tag_name()
                    .name()
                    .eq_ignore_ascii_case("ncx")
                    || doc.descendants().any(|n| n.has_tag_name("navMap"))
            });
        if ncx {
            if let Some(doc) = doc {
                out.extend(ncx_points(&doc).into_iter().enumerate().map(
                    |(idx, (node, depth, parent))| {
                        let title = node
                            .children()
                            .find(|n| n.has_tag_name("navLabel"))
                            .and_then(|l| l.descendants().find(|n| n.has_tag_name("text")))
                            .map(|n| {
                                n.descendants()
                                    .filter(|n| n.is_text())
                                    .filter_map(|n| n.text())
                                    .collect::<String>()
                            })
                            .unwrap_or_default();
                        let href = node
                            .children()
                            .find(|n| n.has_tag_name("content"))
                            .and_then(|n| n.attribute("src"))
                            .unwrap_or("");
                        Toc::new(
                            path,
                            idx,
                            node.attribute("id").unwrap_or(""),
                            parent,
                            depth,
                            "ncx",
                            strip(&title).to_owned(),
                            href,
                        )
                    },
                ));
            }
        } else {
            // Non-NCX navigation is HTML even when it has an unusual suffix.
            let text = if html { text } else { decode(&bytes) };
            let dom = Dom::parse(&text);
            // Every HTML document reaching here is a declared navigation document, so its
            // page labels are captured whatever toc marker (or none) its <nav> carries.
            for node in nav_headings(&dom) {
                let title = dom.text(node, " ", true);
                if !title.is_empty() && !nav_titles.contains(&title) {
                    nav_titles.push(title);
                }
            }
            out.extend(nav_labels(&dom).into_iter().enumerate().map(
                |(idx, (li, label, depth, parent))| {
                    Toc::new(
                        path,
                        idx,
                        dom.attr(li, "id").unwrap_or(""),
                        parent,
                        depth,
                        "nav",
                        dom.text(label, " ", true),
                        if dom.name(label) == "a" {
                            dom.attr(label, "href").unwrap_or("")
                        } else {
                            ""
                        },
                    )
                },
            ));
        }
    }
    Ok(out)
}

#[derive(Serialize)]
struct Chapter {
    index: usize,
    title: String,
    href: String,
    segments: Vec<Segment>,
    meta: Value,
}

#[derive(Serialize)]
struct Document<'a> {
    title: String,
    source_lang: &'a str,
    target_lang: &'a str,
    fmt: &'static str,
    source_path: &'a Path,
    chapters: Vec<Chapter>,
    meta: Value,
}

fn contexts(
    resources: &[Resource],
    spine: usize,
    specs: &[Spec],
    indices: &[FragmentIndex<'_>],
) -> (Value, Vec<(usize, usize, usize)>) {
    let resources_by_href: HashMap<_, _> = specs
        .iter()
        .enumerate()
        .map(|(i, spec)| (spec.href.as_str(), i))
        .collect();
    let mut results = Map::new();
    let mut promote = Vec::new();
    let annotation_nodes: Vec<HashMap<&str, Vec<Id>>> = resources
        .iter()
        .map(|r| {
            let mut index: HashMap<_, Vec<_>> = HashMap::new();
            for node in r.dom.tags(0) {
                if let Some(id) = r.dom.attr(node, ANNOTATION) {
                    index.entry(id).or_default().push(node);
                }
            }
            index
        })
        .collect();
    let sources: Vec<HashMap<&str, &str>> = resources
        .iter()
        .map(|r| {
            r.segments
                .iter()
                .map(|s| (s.anchor.as_str(), s.source.as_str()))
                .collect()
        })
        .collect();
    for (ri, resource) in resources[..spine].iter().enumerate() {
        let mut object_dom = None;
        for (si, segment) in resource.segments.iter().enumerate() {
            let Some(items) = segment.meta["epub_annotations"]["items"].as_array() else {
                continue;
            };
            for (ii, item) in items.iter().enumerate() {
                let relation = item["relation"].as_str().unwrap_or("");
                let Some(key) = item["target_key"].as_str() else {
                    continue;
                };
                if relation == OBJECT {
                    if results.contains_key(key) {
                        continue;
                    }
                    let id = item["id"].as_str().unwrap_or("");
                    if let Some(&[node]) = annotation_nodes[ri].get(id).map(Vec::as_slice) {
                        // Clone only the resource's arena when an actual protected-object snippet is needed.
                        let copy = object_dom.get_or_insert_with(|| resource.dom.clone());
                        let node = copy.copy(node);
                        let nodes: Vec<_> = std::iter::once(node).chain(copy.tags(node)).collect();
                        for n in nodes {
                            for attr in [ANCHOR, ANNOTATION, INLINE] {
                                copy.attrs_mut(n).remove(attr);
                            }
                        }
                        results.insert(key.to_owned(), json!({"target_key": key, "resource_href": segment.resource_href, "fragment": id,
                            "source_blocks": [copy.html(node)], "segment_anchors": []}));
                    }
                    continue;
                }
                if !matches!(relation, "noteref" | "internal_link") {
                    continue;
                }
                let link = resolve(
                    &segment.resource_href,
                    item["raw_href"].as_str().unwrap_or(""),
                );
                if link.external || link.fragment.is_empty() || link.key() != key {
                    continue;
                }
                let Some(&ri_target) = resources_by_href.get(link.path.as_str()) else {
                    continue;
                };
                let index = &indices[ri_target];
                let Some(&[node]) = index.owners.get(link.fragment.as_str()).map(Vec::as_slice)
                else {
                    continue;
                };
                let dom = &resources[ri_target].dom;
                let (scope, note) = index.note_scope(dom, node, &link.fragment);
                if relation == "internal_link" {
                    if !note {
                        continue;
                    }
                    promote.push((ri, si, ii));
                }
                if results.contains_key(key) {
                    continue;
                }
                let mut anchors: Vec<&str> = Vec::new();
                for id in std::iter::once(scope).chain(dom.tags(scope)) {
                    if let Some(anchor) = dom
                        .attr(id, ANCHOR)
                        .filter(|id| sources[ri_target].contains_key(id))
                    {
                        if !anchors.contains(&anchor) {
                            anchors.push(anchor);
                        }
                    }
                }
                if anchors.is_empty() || (ri == ri_target && anchors == [segment.anchor.as_str()]) {
                    continue;
                }
                let blocks: Vec<_> = anchors
                    .iter()
                    .map(|id| sources[ri_target][id])
                    .filter(|s| !strip(s).is_empty())
                    .collect();
                if !blocks.is_empty() {
                    results.insert(key.to_owned(), json!({"target_key": key, "resource_href": link.path,
                        "fragment": link.fragment, "source_blocks": blocks, "segment_anchors": anchors}));
                }
            }
        }
    }
    (json!({"version": 1, "contexts": results}), promote)
}

fn locate_boundaries(
    resources: &[Resource],
    specs: &[Spec],
    indices: &[FragmentIndex<'_>],
    entries: &mut [Toc],
    split_nested_toc: bool,
) -> (String, BTreeMap<usize, usize>) {
    let mut count = 0;
    let mut locations = HashMap::new();
    let mut positions = HashMap::new();
    for (i, (resource, spec)) in resources.iter().zip(specs).enumerate() {
        locations.insert(spec.href.as_str(), (i, count));
        for (offset, seg) in resource.segments.iter().enumerate() {
            positions.insert(seg.anchor.as_str(), count + offset);
        }
        count += resource.segments.len();
    }
    let mut refs = HashMap::new();
    for e in entries.iter() {
        *refs
            .entry((e.toc_path.clone(), e.resource_href.clone()))
            .or_insert(0) += 1;
    }
    for e in entries.iter_mut() {
        let Some(&(i, start)) = locations.get(e.resource_href.as_str()) else {
            continue;
        };
        let res = &resources[i];
        if e.fragment.is_empty() {
            e.boundary_position = Some(start);
            e.segment_anchor = res.segments.first().map(|s| s.anchor.clone());
        } else if let Some(&node) = indices[i].boundaries.get(e.fragment.as_str()) {
            if let Some(anchor) = node
                .and_then(|n| res.dom.attr(n, ANCHOR))
                .filter(|a| positions.contains_key(a))
            {
                e.boundary_position = Some(positions[anchor]);
                e.segment_anchor = Some(anchor.to_owned());
            } else {
                e.boundary_position = Some(start + res.segments.len());
            }
        } else if refs[&(e.toc_path.clone(), e.resource_href.clone())] == 1 {
            e.boundary_position = Some(start);
        }
    }
    let mut groups: IndexMap<String, Vec<usize>> = IndexMap::new();
    for (idx, e) in entries.iter().enumerate() {
        groups.entry(e.toc_path.clone()).or_default().push(idx);
    }
    for group in groups.values() {
        let mut child_boundaries = HashMap::new();
        for &i in group.iter().rev() {
            let e = &mut entries[i];
            if e.boundary_position.is_none() && e.raw_href.is_empty() {
                if let Some((position, id)) = child_boundaries.remove(&e.node_index) {
                    e.boundary_position = Some(position);
                    e.inherited_boundary_from = Some(id);
                }
            }
            if let (Some(parent), Some(position)) = (e.parent_index, e.boundary_position) {
                child_boundaries.insert(parent, (position, e.entry_id.clone()));
            }
        }
    }
    for (path, group) in groups {
        let mut boundaries: BTreeMap<usize, usize> = BTreeMap::new();
        let mut resolved = 0;
        for i in group {
            let e = &entries[i];
            if (!split_nested_toc && e.depth != 0) || e.external {
                continue;
            }
            if let Some(position) = e.boundary_position {
                resolved += 1;
                if boundaries
                    .get(&position)
                    .is_none_or(|&old| entries[old].segment_anchor.is_none())
                {
                    boundaries.insert(position, i);
                }
            }
        }
        if boundaries.is_empty() {
            continue;
        }
        if boundaries.len() == 1
            && resolved >= 2
            && resources
                .iter()
                .filter(|r| !r.segments.is_empty())
                .take(2)
                .count()
                == 2
        {
            boundaries.clear();
        }
        return (path, boundaries);
    }
    (String::new(), BTreeMap::new())
}

pub fn ingest_with_schema(
    path: &Path,
    source_lang: &str,
    target_lang: &str,
    epub_schema: Option<u32>,
) -> Result<Vec<u8>> {
    let schema = epub_schema.unwrap_or(EPUB_SCHEMA);
    let loose_text = schema >= EPUB_SCHEMA;
    let mut zip = open(path)?;
    let package = package(&mut zip)?;
    let mut nav_titles = Vec::new();
    let mut toc = toc_entries(&mut zip, &package.toc_paths, &mut nav_titles)?;
    let spine_paths: HashSet<_> = package.spine.iter().map(|s| s.href.as_str()).collect();
    let mut specs: Vec<_> = package
        .spine
        .iter()
        .filter(|s| zip.index_for_name(&s.href).is_some())
        .cloned()
        .collect();
    let spine = specs.len();
    specs.extend(
        package
            .manifest
            .iter()
            .enumerate()
            .filter(|(_, href)| {
                !spine_paths.contains(href.as_str())
                    && !package.toc_paths.contains(href)
                    && zip.index_for_name(href).is_some()
            })
            .map(|(ordinal, href)| Spec {
                index: package.spine.len() + ordinal,
                href: href.clone(),
            }),
    );
    let inputs = specs
        .iter()
        .map(|spec| Ok((spec, read(&mut zip, &spec.href)?)))
        .collect::<Result<Vec<_>>>()?;
    let mut resources = ordered_map(inputs, |(spec, bytes)| {
        annotate_with_schema(
            &decode(&bytes),
            spec.index,
            &spec.href,
            &package.title,
            package.toc_paths.contains(&spec.href),
            loose_text,
        )
    });
    let indices: Vec<_> = resources
        .iter()
        .map(|r| FragmentIndex::new(&r.dom))
        .collect();
    let (contexts, updates) = contexts(&resources, spine, &specs, &indices);
    let (canonical, boundaries) = locate_boundaries(
        &resources[..spine],
        &specs[..spine],
        &indices[..spine],
        &mut toc,
        package.split_nested_toc,
    );
    drop(indices);
    for (ri, si, ii) in updates {
        resources[ri].segments[si].meta["epub_annotations"]["items"][ii]["relation"] =
            json!("noteref");
    }
    resources.truncate(spine);
    let strategy = if boundaries.is_empty() {
        "spine-fallback"
    } else if package.split_nested_toc {
        ALL_TOC_HEADINGS
    } else {
        "top-level-toc"
    };
    let mut chapters = Vec::new();
    let mut push =
        |title: String, href: String, mut segments: Vec<Segment>, entry_id: Option<&str>| {
            if segments.is_empty() {
                return;
            }
            for (i, seg) in segments.iter_mut().enumerate() {
                seg.index = i;
                seg.meta
                    .as_object_mut()
                    .unwrap()
                    .shift_remove("epub_inline");
            }
            let mut meta = json!({"epub_split_strategy": strategy});
            if let Some(id) = entry_id {
                meta["toc_entry_id"] = json!(id);
            }
            chapters.push(Chapter {
                index: chapters.len(),
                title,
                href,
                segments,
                meta,
            });
        };
    if boundaries.is_empty() {
        for (resource, spec) in resources.into_iter().zip(&specs) {
            push(resource.title, spec.href.clone(), resource.segments, None);
        }
    } else {
        let all: Vec<_> = resources.into_iter().flat_map(|r| r.segments).collect();
        let length = all.len();
        let mut segments = all.into_iter();
        let mut boundaries: Vec<_> = boundaries.into_iter().map(|(p, i)| (p, Some(i))).collect();
        if boundaries[0].0 > 0 {
            boundaries.insert(0, (0, None));
        }
        boundaries.push((length, None));
        for pair in boundaries.windows(2) {
            let (start, index) = pair[0];
            let end = pair[1].0;
            if start >= end {
                continue;
            }
            let chunk: Vec<_> = segments.by_ref().take(end - start).collect();
            let first = &chunk[0];
            let href = first.resource_href.clone();
            let e = index.map(|i| &toc[i]);
            let title = e.map(|e| e.title.clone()).unwrap_or_else(|| {
                if first.kind == "heading" {
                    first.source.clone()
                } else {
                    String::new()
                }
            });
            push(title, href, chunk, e.map(|e| e.entry_id.as_str()));
        }
    }
    let mut meta = json!({"epub_schema": schema, "epub_objects": 1, "opf_path": package.opf, "toc_paths": package.toc_paths,
        "toc_entries": toc, "epub_resources": &specs[..spine], "epub_split_strategy": strategy,
        "epub_split_toc_path": canonical, "epub_annotation_contexts": contexts});
    if let Some(obj) = meta.as_object_mut() {
        if !package.description.is_empty() {
            obj.insert("epub_description".to_owned(), json!(package.description));
        }
        if !package.subjects.is_empty() {
            obj.insert("epub_subjects".to_owned(), json!(package.subjects));
        }
        if !package.creators.is_empty() {
            obj.insert("epub_creators".to_owned(), json!(package.creators));
        }
        if !nav_titles.is_empty() {
            obj.insert("epub_nav_titles".to_owned(), json!(nav_titles));
        }
    }
    Ok(serde_json::to_vec(&Document {
        title: book_title(path, &package),
        source_lang,
        target_lang,
        fmt: "epub",
        source_path: path,
        chapters,
        meta,
    })?)
}
