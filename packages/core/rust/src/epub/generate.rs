//! Construct an EPUB 3 package from rendered chapters and resolved binary assets.

use std::{
    collections::{BTreeSet, HashSet},
    fs::File,
    io::{BufWriter, Write},
    path::Path,
};

use anyhow::{anyhow, bail, Result};
use percent_encoding::{utf8_percent_encode, AsciiSet, NON_ALPHANUMERIC};
use quick_xml::{events::Event, Reader};
use serde::Deserialize;
use zip::{write::SimpleFileOptions, CompressionMethod, ZipWriter};

use super::{
    archive::write_mimetype,
    dom::{Dom, Id},
    export::about_name,
    ingest::xml,
    links::validate_path,
    workers::ordered_map,
};

const XHTML: &str = "http://www.w3.org/1999/xhtml";
const SVG: &str = "http://www.w3.org/2000/svg";
const MATH: &str = "http://www.w3.org/1998/Math/MathML";
const EPUB: &str = "http://www.idpf.org/2007/ops";
const XLINK: &str = "http://www.w3.org/1999/xlink";
const OPF: &str = "http://www.idpf.org/2007/opf";
const DC: &str = "http://purl.org/dc/elements/1.1/";
const NCX: &str = "http://www.daisy.org/z3986/2005/ncx/";
const XML_DECL: &str = "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n";
const URI: &AsciiSet = &NON_ALPHANUMERIC
    .remove(b'-')
    .remove(b'_')
    .remove(b'.')
    .remove(b'~')
    .remove(b'/');
const FRAGMENT_URI: &AsciiSet = &URI.remove(b'#');

#[derive(Deserialize)]
pub struct Input {
    title: String,
    lang: String,
    modified: String,
    chapters: Vec<Chapter>,
    toc: Vec<Entry>,
    #[serde(default)]
    about: Option<String>,
    #[serde(default)]
    bilingual_css: Option<String>,
    #[serde(default)]
    split_nested_toc: bool,
}

#[derive(Deserialize)]
struct Chapter {
    href: String,
    title: String,
    content: String,
}

#[derive(Deserialize)]
struct Entry {
    href: String,
    title: String,
    children: Vec<Entry>,
}

pub type Asset = (String, String, Vec<u8>, bool);

fn child(dom: &mut Dom, parent: Id, tag: &str, text: &str, attrs: &[(&str, &str)]) -> Id {
    let node = dom.tag(tag);
    for &(key, value) in attrs {
        dom.set(node, key, value);
    }
    dom.append_text(node, text);
    dom.append(parent, node);
    node
}

fn binding<'a>(dom: &'a Dom, node: Id, key: &str) -> Option<&'a str> {
    std::iter::once(node)
        .chain(dom.parents(node))
        .find_map(|ancestor| dom.attr(ancestor, key))
}

fn expanded_name(dom: &Dom, id: Id) -> (Option<&str>, &str) {
    let name = dom.name(id);
    if let Some((prefix, local)) = name.split_once(':') {
        (binding(dom, id, &format!("xmlns:{prefix}")), local)
    } else {
        (binding(dom, id, "xmlns"), name)
    }
}

fn xhtml_child(dom: &mut Dom, parent: Id, tag: &str, text: &str) -> Id {
    let node = child(dom, parent, tag, text, &[]);
    if binding(dom, parent, "xmlns") != Some(XHTML) {
        dom.set(node, "xmlns", XHTML);
    }
    node
}

fn normalize_html_namespaces(dom: &mut Dom, html: Id, html_syntax: bool) {
    // The HTML exporter wraps ordinary HTML in an unprefixed XHTML shell. Keep
    // legacy inline SVG/MathML integration in that shell, without reinterpreting
    // prefixed names or overriding explicitly declared element namespaces.
    let mut stack = vec![(html, "", dom.attr(html, "xmlns") == Some(XHTML), false)];
    while let Some((mut id, inherited, infer, from_foreign_object)) = stack.pop() {
        let declared = dom.attr(id, "xmlns").map(|uri| match uri {
            XHTML => XHTML,
            SVG => SVG,
            MATH => MATH,
            _ => "",
        });
        let mut default = declared.unwrap_or(inherited);
        let infer = (infer || from_foreign_object) && declared.is_none_or(|uri| uri == XHTML);
        if infer && declared.is_none() {
            let implied = if from_foreign_object { XHTML } else { default };
            let desired = match (implied, dom.name(id)) {
                (XHTML, "svg") => SVG,
                (XHTML, "math") => MATH,
                _ => implied,
            };
            if desired != default {
                dom.set(id, "xmlns", desired);
                default = desired;
            }
        }
        let name = dom.name(id);
        let (prefix, local) = name.rsplit_once(':').unwrap_or(("", name));
        let integration = (infer || (html_syntax && default == SVG))
            && matches!(local, "foreignObject" | "foreignobject")
            && expanded_name(dom, id).0 == Some(SVG);
        if integration && local == "foreignobject" {
            // The HTML parser case-folds SVG names. Use the XML spelling here.
            let name = if prefix.is_empty() {
                "foreignObject".to_owned()
            } else {
                format!("{prefix}:foreignObject")
            };
            let corrected = dom.tag(&name);
            *dom.attrs_mut(corrected) = std::mem::take(dom.attrs_mut(id));
            let children = dom.children(id).to_vec();
            dom.clear(id);
            for node in children {
                dom.append(corrected, node);
            }
            dom.insert(id, corrected, false);
            dom.detach(id);
            id = corrected;
        }
        stack.extend(
            dom.children(id)
                .iter()
                .rev()
                .copied()
                .filter(|&node| !dom.name(node).is_empty())
                .map(|node| (node, default, infer, integration)),
        );
    }
}

fn has_entity_declaration(content: &str) -> bool {
    if !content.contains("<!ENTITY") {
        return false;
    }
    // A DTD can only occur in the prolog. Inspect its markup, not the body,
    // where comments, CDATA and HTML raw text can contain the same bytes.
    let mut reader = Reader::from_str(content);
    loop {
        match reader.read_event() {
            Ok(Event::DocType(dtd)) => {
                let mut markup = std::str::from_utf8(dtd.as_ref()).unwrap();
                while let Some(i) = markup.find(['<', '\'', '"']) {
                    let tail = &markup[i..];
                    if tail.starts_with("<!ENTITY") {
                        return true;
                    }
                    let skipped = if let Some(body) = tail.strip_prefix("<!--") {
                        body.split_once("-->").map(|(_, rest)| rest)
                    } else if let Some(body) = tail.strip_prefix("<?") {
                        body.split_once("?>").map(|(_, rest)| rest)
                    } else if tail.starts_with(['\'', '"']) {
                        tail[1..].split_once(&tail[..1]).map(|(_, rest)| rest)
                    } else {
                        Some(&tail[1..])
                    };
                    let Some(rest) = skipped else { return true };
                    markup = rest;
                }
            }
            Ok(Event::Start(_) | Event::Empty(_) | Event::Eof) => return false,
            Err(_) => return true,
            _ => {}
        }
    }
}

fn content_properties(doc: &roxmltree::Document<'_>) -> String {
    let mut properties = BTreeSet::new();
    for node in doc.descendants().filter(roxmltree::Node::is_element) {
        let name = node.tag_name();
        match name.namespace() {
            Some(SVG) => properties.insert("svg"),
            Some(MATH) => properties.insert("mathml"),
            _ => false,
        };
        if matches!(
            (name.namespace(), name.name()),
            (
                Some(XHTML),
                "img" | "audio" | "video" | "source" | "image" | "link" | "script"
            ) | (Some(SVG), "image" | "script")
        ) && node.attributes().any(|attr| {
            matches!(
                (attr.namespace(), attr.name()),
                (None, "src" | "href" | "poster") | (Some(XLINK), "href")
            ) && ["http://", "https://", "//"]
                .iter()
                .any(|prefix| attr.value().starts_with(prefix))
        }) {
            properties.insert("remote-resources");
        }
        let scripted = match (name.namespace(), name.name()) {
            (Some(XHTML | SVG), "script") => true,
            (Some(XHTML | SVG | MATH), _) => node
                .attributes()
                .any(|attr| attr.namespace().is_none() && attr.name().starts_with("on")),
            _ => false,
        };
        if scripted {
            properties.insert("scripted");
        }
    }
    properties.into_iter().collect::<Vec<_>>().join(" ")
}

fn document(
    chapter: &Chapter,
    lang: &str,
    bilingual_css: Option<&str>,
) -> Result<(String, String)> {
    // Generated chapters never need a DTD or any custom entities.
    if has_entity_declaration(&chapter.content) {
        bail!("EPUB chapters must not contain unresolved XML entities");
    }
    let (mut dom, parsed_as_html) = match xml(&chapter.content) {
        Ok(doc) => (Dom::from_xml(doc.root_element()), false),
        Err(_) => (Dom::parse(&chapter.content), true),
    };
    let html = dom
        .children(0)
        .iter()
        .copied()
        .find(|&id| !dom.name(id).is_empty())
        .ok_or_else(|| anyhow!("EPUB chapters must be complete HTML documents"))?;
    let plain_html = dom.name(html) == "html" && dom.attr(html, "xmlns").is_none();
    if plain_html {
        dom.set(html, "xmlns", XHTML);
    }
    if expanded_name(&dom, html) != (Some(XHTML), "html") {
        bail!("EPUB chapters must be complete HTML documents");
    }
    for (key, uri) in [("xmlns:epub", EPUB), ("xmlns:xlink", XLINK)] {
        if dom.attr(html, key).is_none() {
            dom.set(html, key, uri);
        }
    }
    normalize_html_namespaces(&mut dom, html, plain_html || parsed_as_html);
    let direct = |dom: &Dom, parent, local| {
        dom.children(parent)
            .iter()
            .copied()
            .find(|&id| expanded_name(dom, id) == (Some(XHTML), local))
    };
    if direct(&dom, html, "body").is_none() {
        bail!("EPUB chapters must contain a body");
    }
    let head = direct(&dom, html, "head").unwrap_or_else(|| {
        let head = xhtml_child(&mut dom, html, "head", "");
        dom.prepend(html, head);
        head
    });
    let titles: Vec<_> = dom
        .children(head)
        .iter()
        .copied()
        .filter(|&n| expanded_name(&dom, n) == (Some(XHTML), "title"))
        .collect();
    for id in titles {
        dom.detach(id);
    }
    xhtml_child(&mut dom, head, "title", &chapter.title);
    dom.set(html, "lang", lang);
    dom.set(html, "xml:lang", lang);
    if let Some(css) = bilingual_css.filter(|_| {
        !std::iter::once(html)
            .chain(dom.tags(html))
            .any(|id| dom.attr(id, "id") == Some("tn-bilingual-style"))
    }) {
        let style = xhtml_child(&mut dom, head, "style", css);
        dom.set(style, "id", "tn-bilingual-style");
    }
    let result = format!("{XML_DECL}{}", dom.xml(html));
    // Validate prefix bindings and derive manifest properties from the expanded
    // names that will actually be written into the ZIP.
    let parsed = xml(&result)
        .map_err(|e| anyhow!("Cannot serialize EPUB chapter {}: {e:#}", chapter.href))?;
    let properties = content_properties(&parsed);
    Ok((result, properties))
}

fn toc(title: &str, lang: &str, identifier: &str, entries: &[Entry]) -> (String, String) {
    let mut nav_doc = Dom::new();
    let html = child(
        &mut nav_doc,
        0,
        "html",
        "",
        &[
            ("xmlns", XHTML),
            ("xmlns:epub", EPUB),
            ("lang", lang),
            ("xml:lang", lang),
        ],
    );
    let head = child(&mut nav_doc, html, "head", "", &[]);
    child(&mut nav_doc, head, "title", title, &[]);
    let body = child(&mut nav_doc, html, "body", "", &[]);
    let nav = child(
        &mut nav_doc,
        body,
        "nav",
        "",
        &[("id", "toc"), ("epub:type", "toc")],
    );
    child(&mut nav_doc, nav, "h1", title, &[]);
    let ol = child(&mut nav_doc, nav, "ol", "", &[]);

    let mut ncx_doc = Dom::new();
    let ncx = child(
        &mut ncx_doc,
        0,
        "ncx",
        "",
        &[("xmlns", NCX), ("version", "2005-1")],
    );
    let head = child(&mut ncx_doc, ncx, "head", "", &[]);
    child(
        &mut ncx_doc,
        head,
        "meta",
        "",
        &[("name", "dtb:uid"), ("content", identifier)],
    );
    let title_el = child(&mut ncx_doc, ncx, "docTitle", "", &[]);
    child(&mut ncx_doc, title_el, "text", title, &[]);
    let map = child(&mut ncx_doc, ncx, "navMap", "", &[]);
    let mut stack: Vec<_> = entries.iter().rev().map(|e| (ol, map, e, 1)).collect();
    let (mut ordinal, mut depth) = (0, 1);
    while let Some((parent, point, entry, level)) = stack.pop() {
        ordinal += 1;
        depth = depth.max(level);
        let href = utf8_percent_encode(&entry.href, FRAGMENT_URI).to_string();
        let li = child(&mut nav_doc, parent, "li", "", &[]);
        child(&mut nav_doc, li, "a", &entry.title, &[("href", &href)]);
        let point = child(
            &mut ncx_doc,
            point,
            "navPoint",
            "",
            &[
                ("id", &format!("nav-{ordinal}")),
                ("playOrder", &ordinal.to_string()),
            ],
        );
        let label = child(&mut ncx_doc, point, "navLabel", "", &[]);
        child(&mut ncx_doc, label, "text", &entry.title, &[]);
        child(&mut ncx_doc, point, "content", "", &[("src", &href)]);
        if !entry.children.is_empty() {
            let list = child(&mut nav_doc, li, "ol", "", &[]);
            stack.extend(
                entry
                    .children
                    .iter()
                    .rev()
                    .map(|e| (list, point, e, level + 1)),
            );
        }
    }
    for (name, content) in [
        ("dtb:depth", depth.to_string()),
        ("dtb:totalPageCount", "0".into()),
        ("dtb:maxPageNumber", "0".into()),
    ] {
        child(
            &mut ncx_doc,
            head,
            "meta",
            "",
            &[("name", name), ("content", &content)],
        );
    }
    (
        format!("{XML_DECL}{}", nav_doc.xml(0)),
        format!("{XML_DECL}{}", ncx_doc.xml(0)),
    )
}

pub fn generate(destination: &Path, mut input: Input, assets: Vec<Asset>) -> Result<()> {
    if input.chapters.is_empty() {
        bail!("Cannot generate an EPUB without chapters");
    }
    if input.toc.is_empty() {
        bail!("Cannot generate an EPUB without navigation entries");
    }
    let mut used = HashSet::from(["content.opf", "nav.xhtml", "toc.ncx", "cover.xhtml"]);
    for path in input
        .chapters
        .iter()
        .map(|c| c.href.as_str())
        .chain(assets.iter().map(|(p, ..)| p.as_str()))
    {
        validate_path(path)?;
        if !used.insert(path) {
            bail!("Duplicate EPUB resource path: {path}");
        }
    }
    let chapters: HashSet<_> = input.chapters.iter().map(|c| c.href.as_str()).collect();
    let mut todo: Vec<_> = input.toc.iter().collect();
    while let Some(entry) = todo.pop() {
        if !chapters.contains(entry.href.split('#').next().unwrap_or_default()) {
            bail!(
                "EPUB navigation refers to a missing chapter: {}",
                entry.href
            );
        }
        todo.extend(&entry.children);
    }
    if assets.iter().filter(|a| a.3).take(2).count() > 1 {
        bail!("An EPUB can have only one cover image");
    }
    let about = input.about.take().map(|text| {
        let mut number = 0;
        loop {
            let href = about_name(number);
            if !used.contains(href.as_str()) {
                break (href, text);
            }
            number += 1;
        }
    });
    let body = ordered_map(input.chapters, |chapter| {
        let (xml, props) = document(&chapter, &input.lang, input.bilingual_css.as_deref())?;
        Ok((chapter.href, xml, props))
    })
    .into_iter()
    .collect::<Result<Vec<_>>>()?;
    let identifier = format!("trans-novel-{}", input.title);
    let (nav, ncx) = toc(&input.title, &input.lang, &identifier, &input.toc);
    let mut dom = Dom::new();
    let package = child(
        &mut dom,
        0,
        "package",
        "",
        &[
            ("xmlns", OPF),
            ("version", "3.0"),
            ("unique-identifier", "book-id"),
        ],
    );
    let meta = child(&mut dom, package, "metadata", "", &[("xmlns:dc", DC)]);
    if input.split_nested_toc {
        dom.set(
            package,
            "prefix",
            "wenyi: https://bigdawnghost.github.io/wenyi/vocab#",
        );
        child(
            &mut dom,
            meta,
            "meta",
            super::ingest::ALL_TOC_HEADINGS,
            &[("property", super::ingest::BOUNDARY_POLICY)],
        );
    }
    child(
        &mut dom,
        meta,
        "dc:identifier",
        &identifier,
        &[("id", "book-id")],
    );
    child(&mut dom, meta, "dc:title", &input.title, &[]);
    child(&mut dom, meta, "dc:language", &input.lang, &[]);
    child(
        &mut dom,
        meta,
        "meta",
        &input.modified,
        &[("property", "dcterms:modified")],
    );
    let manifest = child(&mut dom, package, "manifest", "", &[]);
    let spine = child(&mut dom, package, "spine", "", &[("toc", "ncx")]);
    let item = |dom: &mut Dom, id: &str, href: &str, media: &str, props: &str| {
        let href = utf8_percent_encode(href, URI).to_string();
        let node = child(
            dom,
            manifest,
            "item",
            "",
            &[("id", id), ("href", &href), ("media-type", media)],
        );
        if !props.is_empty() {
            dom.set(node, "properties", props);
        }
    };
    item(&mut dom, "nav", "nav.xhtml", "application/xhtml+xml", "nav");
    item(&mut dom, "ncx", "toc.ncx", "application/x-dtbncx+xml", "");
    if !input.split_nested_toc {
        // A generated source edition uses the NAV for navigation only. Its
        // display title must not become a chapter preceding the actual book.
        child(&mut dom, spine, "itemref", "", &[("idref", "nav")]);
    }
    let mut out = ZipWriter::new(BufWriter::new(File::create(destination)?));
    write_mimetype(&mut out, b"application/epub+zip")?;
    let mut put = |name: &str, data: &[u8]| -> Result<()> {
        out.start_file(
            name,
            SimpleFileOptions::default()
                .compression_method(CompressionMethod::Deflated)
                .compression_level(Some(3)),
        )?;
        out.write_all(data)?;
        Ok(())
    };
    put("META-INF/container.xml", br#"<?xml version="1.0" encoding="utf-8"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0"><rootfiles><rootfile full-path="EPUB/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>"#)?;
    put("EPUB/nav.xhtml", nav.as_bytes())?;
    put("EPUB/toc.ncx", ncx.as_bytes())?;
    for (index, (href, xml, props)) in body.into_iter().enumerate() {
        let id = format!("chapter-{index}");
        item(&mut dom, &id, &href, "application/xhtml+xml", &props);
        child(&mut dom, spine, "itemref", "", &[("idref", &id)]);
        put(&format!("EPUB/{href}"), xml.as_bytes())?;
    }
    for (index, (href, media, data, cover)) in assets.into_iter().enumerate() {
        let id = format!("resource-{index}");
        item(
            &mut dom,
            &id,
            &href,
            &media,
            if cover { "cover-image" } else { "" },
        );
        put(&format!("EPUB/{href}"), &data)?;
        if cover {
            child(
                &mut dom,
                meta,
                "meta",
                "",
                &[("name", "cover"), ("content", &id)],
            );
            let chapter = Chapter {
                href: "cover.xhtml".to_owned(),
                title: input.title.clone(),
                content: format!(
                    "<html><head></head><body><img alt=\"Cover\" src=\"{}\"/></body></html>",
                    utf8_percent_encode(&href, URI)
                ),
            };
            let (xml, _) = document(&chapter, &input.lang, None)?;
            put("EPUB/cover.xhtml", xml.as_bytes())?;
            item(
                &mut dom,
                "cover",
                "cover.xhtml",
                "application/xhtml+xml",
                "",
            );
        }
    }
    if let Some((href, text)) = about {
        item(
            &mut dom,
            "trans-novel-about",
            &href,
            "application/xhtml+xml",
            "",
        );
        child(
            &mut dom,
            spine,
            "itemref",
            "",
            &[("idref", "trans-novel-about")],
        );
        put(&format!("EPUB/{href}"), text.as_bytes())?;
    }
    put(
        "EPUB/content.opf",
        format!("{XML_DECL}{}", dom.xml(0)).as_bytes(),
    )?;
    out.finish()?.flush()?;
    Ok(())
}
