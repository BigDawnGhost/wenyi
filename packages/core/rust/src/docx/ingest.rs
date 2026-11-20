use std::{
    collections::{BTreeMap, HashMap, HashSet},
    path::Path,
    sync::LazyLock,
};

use anyhow::{ensure, Context, Result};
use regex::Regex;
use roxmltree::Document;
use serde::Serialize;
use serde_json::{json, Map, Value};

use super::{
    has_list_prefix,
    opc::{Package, Relations},
    properties::{
        attr, attrs, child, is, val, Levels, Lists, ParagraphStyle, RunStyle, SourceRun, Span,
        Word, FONTS,
    },
};

fn read_related(
    zip: &mut Package,
    relations: Option<&Relations<'_>>,
    kind: &str,
) -> Result<Option<String>> {
    relations
        .map(|relations| relations.part(kind))
        .transpose()?
        .flatten()
        .map(|uri| zip.xml(&zip.part_name(&uri)))
        .transpose()
}

fn parse_xml(text: &str) -> Result<Document<'_>> {
    Document::parse(text).context("Invalid DOCX XML")
}

pub(super) fn parse(
    file: &Path,
    title: &str,
    source_path: &str,
    source_lang: &str,
    target_lang: &str,
) -> Result<Vec<u8>> {
    let mut zip = Package::open(file)?;
    let root_xml = zip.xml("_rels/.rels")?;
    let root_rel = Relations::new("", &root_xml)?;
    let uri = root_rel
        .part("officeDocument")?
        .context("No main Word document part")?;
    let member = zip.part_name(&uri);
    let content = zip.xml(&member)?;
    let doc = parse_xml(&content)?;
    let root = doc.root_element();
    ensure!(is(root, "document"), "Not a WordprocessingML document");
    let body = child(root, "body").context("Missing Word document body")?;

    // Legacy packages spell the .rels member like the physical owner, but
    // relative targets still resolve against the owner's URI.
    let rel_name = Relations::name(&member);
    let rel_xml = match zip.xml(&rel_name) {
        Ok(xml) => Some(xml),
        Err(error)
            if error
                .downcast_ref::<zip::result::ZipError>()
                .is_some_and(|e| matches!(e, zip::result::ZipError::FileNotFound)) =>
        {
            None
        }
        Err(error) => return Err(error),
    };
    let relations = rel_xml
        .as_deref()
        .map(|xml| Relations::new(&uri, xml))
        .transpose()?;
    let styles_xml = read_related(&mut zip, relations.as_ref(), "styles")?;
    let numbering_xml = read_related(&mut zip, relations.as_ref(), "numbering")?;
    let core_xml = read_related(&mut zip, Some(&root_rel), "core-properties")?;
    let styles_doc = styles_xml.as_deref().map(parse_xml).transpose()?;
    let numbering_doc = numbering_xml.as_deref().map(parse_xml).transpose()?;
    let core_doc = core_xml.as_deref().map(parse_xml).transpose()?;
    let styles = Styles::new(styles_doc.as_ref());
    let lists = numbering_doc.as_ref().map(numbering).unwrap_or_default();
    let mut parser = Parser {
        styles,
        lists,
        used: HashSet::new(),
    };

    let mut chapters = Vec::new();
    let mut current = Chapter::new(title, false);
    let mut table_id = 0;
    for node in body
        .descendants()
        .skip(1)
        .filter(|&node| is(node, "p") || is(node, "tbl"))
    {
        // Unwrap content controls and tracked insertions, but not deleted content,
        // drawing text boxes, or table descendants at body scope.
        if !node
            .ancestors()
            .skip(1)
            .take_while(|ancestor| *ancestor != body)
            .all(|ancestor| {
                ["sdt", "sdtContent", "customXml", "ins", "moveTo"]
                    .iter()
                    .any(|tag| is(ancestor, tag))
            })
        {
            continue;
        }
        if is(node, "p") {
            let paragraph = parser.paragraph(node);
            if paragraph.text.is_empty() {
                continue;
            }
            if paragraph.heading == Some(1) {
                if !current.segments.is_empty() {
                    chapters.push(current);
                }
                current = Chapter::new(&paragraph.text, true);
            }
            let kind = if paragraph.heading.is_some() {
                "heading"
            } else {
                "text"
            };
            let meta = paragraph.meta();
            current.push(paragraph.text, kind, meta);
        } else if parser.table(node, table_id, &mut current)? {
            table_id += 1;
        }
    }
    if !current.segments.is_empty() {
        chapters.push(current);
    }
    ensure!(
        !chapters.is_empty(),
        "No translatable paragraphs or tables found in the Word document"
    );
    for (index, chapter) in chapters.iter_mut().enumerate() {
        chapter.index = index;
    }
    let mut meta = Map::new();
    parser.lists.retain(|id, _| parser.used.contains(id));
    if !parser.lists.is_empty() {
        meta.insert("docx_numbering".into(), json!(parser.lists));
    }
    if let Some(core) = core_doc {
        let properties = core
            .root_element()
            .children()
            .filter_map(|node| {
                let name = node.tag_name().name();
                CORE_PROPERTIES
                    .iter()
                    .any(|&(_, field, ns)| field == name && Some(ns) == node.tag_name().namespace())
                    .then(|| Some((name.to_owned(), Value::String(node.text()?.to_owned()))))
                    .flatten()
            })
            .collect::<Map<_, _>>();
        if !properties.is_empty() {
            meta.insert("docx_core_properties".into(), Value::Object(properties));
        }
    }
    #[derive(Serialize)]
    struct Book<'a> {
        title: &'a str,
        source_path: &'a str,
        fmt: &'static str,
        source_lang: &'a str,
        target_lang: &'a str,
        chapters: Vec<Chapter>,
        meta: Map<String, Value>,
    }
    Ok(serde_json::to_vec(&Book {
        title,
        source_path,
        fmt: "docx",
        source_lang,
        target_lang,
        chapters,
        meta,
    })?)
}

use super::properties::CORE_PROPERTIES;

#[derive(Serialize)]
struct Chapter {
    index: usize,
    title: String,
    segments: Vec<Segment>,
    meta: ChapterMeta,
}

#[derive(Serialize)]
struct ChapterMeta {
    heading_level: u8,
    explicit_title: bool,
}

#[derive(Serialize)]
struct Segment {
    index: usize,
    source: String,
    kind: &'static str,
    meta: Map<String, Value>,
}

impl Chapter {
    fn new(title: &str, explicit_title: bool) -> Self {
        Self {
            index: 0,
            title: title.to_owned(),
            segments: Vec::new(),
            meta: ChapterMeta {
                heading_level: 1,
                explicit_title,
            },
        }
    }

    fn push(&mut self, source: String, kind: &'static str, meta: Map<String, Value>) {
        self.segments.push(Segment {
            index: self.segments.len(),
            source,
            kind,
            meta,
        });
    }
}

struct Styles<'a, 'input> {
    by_id: HashMap<&'a str, Word<'a, 'input>>,
    default_paragraph: Option<&'a str>,
}

impl<'a, 'input: 'a> Styles<'a, 'input> {
    fn new(document: Option<&'a Document<'input>>) -> Self {
        let mut styles = Self {
            by_id: HashMap::new(),
            default_paragraph: None,
        };
        if let Some(doc) = document {
            for node in doc.root_element().children().filter(|&n| is(n, "style")) {
                if let Some(id) = attr(node, "styleId") {
                    styles.by_id.insert(id, node);
                    if attr(node, "type") == Some("paragraph")
                        && matches!(attr(node, "default"), Some("1" | "true" | "on"))
                    {
                        styles.default_paragraph = Some(id);
                    }
                }
            }
        }
        styles
    }

    // Resolve base styles first. The bound also protects against cycles in malformed documents.
    fn chain(&self, id: Option<&str>) -> Vec<Word<'a, 'input>> {
        let mut node = id.and_then(|id| self.by_id.get(id)).copied();
        let mut result = Vec::new();
        while let Some(style) = node {
            if result.len() == 32 || result.contains(&style) {
                break;
            }
            result.push(style);
            node = val(style, "basedOn")
                .and_then(|base| self.by_id.get(base))
                .copied();
        }
        result.reverse();
        result
    }
}

fn numbering(document: &Document<'_>) -> Lists {
    let root = document.root_element();
    let mut abstracts: HashMap<_, Levels> = HashMap::new();
    for node in root.children().filter(|&n| is(n, "abstractNum")) {
        if let Some(id) = attr(node, "abstractNumId") {
            let mut levels = Levels::new();
            for lvl in node.children().filter(|&n| is(n, "lvl")) {
                if let Some(index) = attr(lvl, "ilvl")
                    .and_then(|s| s.parse::<u8>().ok())
                    .filter(|&n| n < 9)
                {
                    levels.entry(index).or_default().apply(lvl);
                }
            }
            abstracts.insert(id, levels);
        }
    }
    let mut instances = Lists::new();
    for node in root.children().filter(|&n| is(n, "num")) {
        let Some(num_id) = attr(node, "numId").and_then(|s| s.parse().ok()) else {
            continue;
        };
        let mut levels = val(node, "abstractNumId")
            .and_then(|id| abstracts.get(id))
            .cloned()
            .unwrap_or_default();
        for over in node.children().filter(|&n| is(n, "lvlOverride")) {
            if let Some(index) = attr(over, "ilvl")
                .and_then(|s| s.parse::<u8>().ok())
                .filter(|&n| n < 9)
            {
                let level = levels.entry(index).or_default();
                if let Some(lvl) = child(over, "lvl") {
                    level.apply(lvl);
                }
                level.start_override = val(over, "startOverride").and_then(|s| s.parse().ok());
            }
        }
        instances.insert(num_id, levels);
    }
    instances
}

#[derive(Default)]
struct Paragraph {
    text: String,
    heading: Option<u8>,
    style: ParagraphStyle,
    list: Option<(i64, u8, String)>,
    uniform: Option<RunStyle>,
    mixed: Vec<(usize, Span<RunStyle>)>,
    source_runs: Vec<Span<SourceRun>>,
}

impl Paragraph {
    fn meta(&self) -> Map<String, Value> {
        let Value::Object(mut meta) = json!(self.style) else {
            unreachable!()
        };
        if let Some(level) = self.heading {
            meta.insert("heading_level".into(), json!(level));
        }
        if let Some((id, level, fmt)) = &self.list {
            meta.insert("list_num_id".into(), json!(id));
            meta.insert("list_ilvl".into(), json!(level));
            meta.insert("list_fmt".into(), json!(fmt));
        }
        if let Some(style) = &self.uniform {
            meta.insert("docx_style".into(), json!(style));
        } else if !self.mixed.is_empty() {
            let items: Vec<_> = self
                .mixed
                .iter()
                .map(|(i, span)| {
                    let Value::Object(mut obj) = json!(span) else {
                        unreachable!()
                    };
                    obj.insert("id".into(), json!(format!("s{i}")));
                    obj.insert("mode".into(), json!("range"));
                    Value::Object(obj)
                })
                .collect();
            meta.insert("docx_styles".into(), json!({"items":items}));
        }
        if !self.source_runs.is_empty() {
            meta.insert("docx_source_runs".into(), json!(self.source_runs));
        }
        meta
    }
}

struct Parser<'a, 'input> {
    styles: Styles<'a, 'input>,
    lists: Lists,
    used: HashSet<i64>,
}

fn named_level(name: &str, kind: &str) -> Option<u8> {
    static HEADING: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"(?i)^(?:heading|标题|標題)\s*([1-9])$").unwrap());
    static TOC: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?i)^toc\s*([1-9])$").unwrap());
    let expr = if kind == "heading" { &*HEADING } else { &*TOC };
    expr.captures(name.trim())?[1].parse().ok()
}

fn run_text(run: Word<'_, '_>, text: &mut String) {
    for n in run.children() {
        match n.tag_name().name() {
            "t" if is(n, "t") => text.push_str(n.text().unwrap_or("")),
            "tab" | "ptab" if super::properties::ns(n).is_some() => text.push('\t'),
            "br" if is(n, "br") && matches!(attr(n, "type"), None | Some("textWrapping")) => {
                text.push('\n')
            }
            "cr" if is(n, "cr") => text.push('\n'),
            "noBreakHyphen" if is(n, "noBreakHyphen") => text.push('-'),
            _ => (),
        }
    }
}

fn run_props(node: Word<'_, '_>, style: &mut RunStyle, source: &mut SourceRun) {
    style.apply(node);
    source.size_pt = style.size_pt;
    if let Some(font) = child(node, "rFonts") {
        source.fonts.extend(attrs(font, FONTS));
    }
}

fn trim_span<T>(start: usize, end: usize, span: &mut Span<T>) -> bool {
    span.source_start = span.source_start.saturating_sub(start).min(end);
    span.source_end = span.source_end.saturating_sub(start).min(end);
    span.source_start < span.source_end
}

impl Parser<'_, '_> {
    fn paragraph(&mut self, node: Word<'_, '_>) -> Paragraph {
        let mut paragraph = Paragraph::default();
        let ppr = child(node, "pPr");
        let style_id = ppr
            .and_then(|n| val(n, "pStyle"))
            .or(self.styles.default_paragraph);
        let chain = self.styles.chain(style_id);
        let mut base_run = RunStyle::default();
        let mut base_source = SourceRun::default();
        let mut outline: Option<u8> = None;
        let mut num_id = None;
        let mut ilvl = None;
        for style in &chain {
            if let Some(name) = val(*style, "name") {
                outline = named_level(name, "heading").or(outline);
                paragraph.style.docx_toc_level =
                    named_level(name, "toc").or(paragraph.style.docx_toc_level);
            }
            if let Some(rpr) = child(*style, "rPr") {
                run_props(rpr, &mut base_run, &mut base_source);
            }
        }
        for properties in chain.iter().filter_map(|n| child(*n, "pPr")).chain(ppr) {
            paragraph.style.apply(properties);
            if let Some(value) = val(properties, "outlineLvl").and_then(|s| s.parse::<u8>().ok()) {
                outline = Some(value.saturating_add(1));
            }
            if let Some(num) = child(properties, "numPr") {
                num_id = val(num, "numId")
                    .and_then(|s| s.parse::<i64>().ok())
                    .or(num_id);
                ilvl = val(num, "ilvl")
                    .and_then(|s| s.parse::<i32>().ok())
                    .map(|n| n.clamp(0, 8) as u8)
                    .or(ilvl);
            }
        }
        paragraph.style.docx_toc_level = paragraph
            .style
            .docx_toc_level
            .or_else(|| named_level(style_id?, "toc"));
        paragraph.heading =
            outline.filter(|&lvl| lvl <= 9 && paragraph.style.docx_toc_level.is_none());

        let mut spans: Vec<Span<RunStyle>> = Vec::new();
        let mut pos = 0;
        for run in node.descendants().filter(|&child| is(child, "r")) {
            if !run
                .ancestors()
                .skip(1)
                .take_while(|ancestor| *ancestor != node)
                .all(|ancestor| {
                    [
                        "hyperlink",
                        "fldSimple",
                        "sdt",
                        "sdtContent",
                        "ins",
                        "moveTo",
                        "smartTag",
                        "customXml",
                    ]
                    .iter()
                    .any(|tag| is(ancestor, tag))
                })
            {
                continue;
            }
            let start = paragraph.text.len();
            run_text(run, &mut paragraph.text);
            if start == paragraph.text.len() {
                continue;
            }
            let mut style = base_run.clone();
            let mut source = base_source.clone();
            if let Some(rpr) = child(run, "rPr") {
                for style_node in self.styles.chain(val(rpr, "rStyle")) {
                    if let Some(props) = child(style_node, "rPr") {
                        run_props(props, &mut style, &mut source);
                    }
                }
                run_props(rpr, &mut style, &mut source);
            }
            let end = pos + paragraph.text[start..].chars().count();
            if let Some(last) = spans
                .last_mut()
                .filter(|last| last.style.same_visible(&style))
            {
                last.source_end = end;
                last.style.size_pt = last.style.size_pt.or(style.size_pt);
            } else {
                spans.push(Span {
                    source_start: pos,
                    source_end: end,
                    style,
                });
            }
            if !source.empty() {
                if let Some(last) = paragraph
                    .source_runs
                    .last_mut()
                    .filter(|last| last.source_end == pos && last.style == source)
                {
                    last.source_end = end;
                } else {
                    paragraph.source_runs.push(Span {
                        source_start: pos,
                        source_end: end,
                        style: source,
                    });
                }
            }
            pos = end;
        }
        let trimmed = paragraph.text.trim();
        if trimmed.len() != paragraph.text.len() {
            let start = (paragraph.text.len() - paragraph.text.trim_start().len())
                .min(paragraph.text.len());
            let leading = paragraph.text[..start].chars().count();
            let len = trimmed.chars().count();
            spans.retain_mut(|span| trim_span(leading, len, span));
            paragraph
                .source_runs
                .retain_mut(|span| trim_span(leading, len, span));
            paragraph.text = trimmed.to_owned();
        }
        if spans.len() == 1 {
            paragraph.uniform = spans
                .pop()
                .map(|span| span.style)
                .filter(|style| !style.empty());
        } else {
            paragraph.mixed = spans
                .into_iter()
                .enumerate()
                .filter(|(_, span)| span.style.visible())
                .collect();
        }
        if let Some(id) = num_id.filter(|&id| id > 0 && !has_list_prefix(&paragraph.text)) {
            let levels = self.lists.get(&id);
            let level = ilvl
                .or_else(|| {
                    levels?.iter().find_map(|(&i, level)| {
                        let id = level.p_style.as_deref()?;
                        (Some(id) == style_id
                            || chain.iter().any(|&node| attr(node, "styleId") == Some(id)))
                        .then_some(i)
                    })
                })
                .unwrap_or_default();
            let fmt = levels
                .and_then(|levels| levels.get(&level))
                .and_then(|level| level.spec.get("numFmt"))
                .map_or("decimal", String::as_str);
            self.used.insert(id);
            paragraph.list = Some((id, level, fmt.to_owned()));
        }
        paragraph
    }

    fn cell(&mut self, node: Word<'_, '_>) -> Paragraph {
        let mut paragraphs = node
            .children()
            .filter(|&n| is(n, "p"))
            .map(|n| self.paragraph(n))
            .filter(|p| !p.text.is_empty());
        let Some(mut first) = paragraphs.next() else {
            return Paragraph::default();
        };
        first.heading = None;
        let mut offset = first.text.chars().count() + 1;
        for mut p in paragraphs {
            if first.uniform != p.uniform || !p.mixed.is_empty() || !first.mixed.is_empty() {
                if let Some(style) = first.uniform.take() {
                    first.mixed.push((
                        0,
                        Span {
                            source_start: 0,
                            source_end: offset - 1,
                            style,
                        },
                    ));
                }
                if let Some(style) = p.uniform.take() {
                    p.mixed.push((
                        0,
                        Span {
                            source_start: 0,
                            source_end: p.text.chars().count(),
                            style,
                        },
                    ));
                }
                let n = first.mixed.len();
                first.mixed.extend(
                    shift(p.mixed.into_iter().map(|(_, span)| span), offset)
                        .enumerate()
                        .map(|(i, span)| (n + i, span)),
                );
            }
            first.source_runs.extend(shift(p.source_runs, offset));
            first.text.push('\n');
            first.text.push_str(&p.text);
            offset += p.text.chars().count() + 1;
        }
        for (index, (id, _)) in first.mixed.iter_mut().enumerate() {
            *id = index;
        }
        first
    }

    fn table(&mut self, table: Word<'_, '_>, id: usize, chapter: &mut Chapter) -> Result<bool> {
        struct Cell<'a, 'input> {
            node: Word<'a, 'input>,
            width: usize,
            height: usize,
        }
        let mut cells = BTreeMap::new();
        let mut active: HashMap<(usize, usize), usize> = HashMap::new();
        let mut rows = 0;
        let mut cols = child(table, "tblGrid").map_or(0, |grid| {
            grid.children().filter(|&n| is(n, "gridCol")).count()
        });
        for row in table.children().filter(|&n| is(n, "tr")) {
            let mut col = child(row, "trPr")
                .and_then(|n| val(n, "gridBefore"))
                .and_then(|s| s.parse::<usize>().ok())
                .unwrap_or(0);
            let mut next_active = HashMap::new();
            for node in row.children().filter(|&n| is(n, "tc")) {
                let props = child(node, "tcPr");
                let width = props
                    .and_then(|p| val(p, "gridSpan"))
                    .and_then(|s| s.parse::<usize>().ok())
                    .unwrap_or(1)
                    .max(1);
                let merge = props.and_then(|p| child(p, "vMerge"));
                let anchor = merge
                    .filter(|&m| attr(m, "val") != Some("restart"))
                    .and_then(|_| active.get(&(col, width)))
                    .copied();
                if let Some(start) = anchor {
                    let cell: &mut Cell<'_, '_> =
                        cells.get_mut(&(start, col)).expect("active merge anchor");
                    cell.height += 1;
                    next_active.insert((col, width), start);
                } else {
                    cells.insert(
                        (rows, col),
                        Cell {
                            node,
                            width,
                            height: 1,
                        },
                    );
                    if merge.is_some() {
                        next_active.insert((col, width), rows);
                    }
                }
                col = col.checked_add(width).context("DOCX table is too wide")?;
            }
            rows += 1;
            cols = cols.max(col);
            active = next_active;
        }
        if rows == 0 || cols == 0 {
            return Ok(false);
        }
        ensure!(
            rows.saturating_mul(cols) <= 1_000_000,
            "DOCX table grid is too large"
        );
        let mut covered = HashSet::new();
        for row in 0..rows {
            for col in 0..cols {
                let (text, mut meta) = if let Some(cell) = cells.get(&(row, col)) {
                    let p = self.cell(cell.node);
                    let mut meta = p.meta();
                    if cell.width > 1 || cell.height > 1 {
                        meta.insert("docx_grid_span".into(), json!(cell.width));
                        meta.insert("docx_row_span".into(), json!(cell.height));
                        for r in row..row + cell.height {
                            for c in col..col + cell.width {
                                if (r, c) != (row, col) {
                                    covered.insert((r, c));
                                }
                            }
                        }
                    }
                    (p.text, meta)
                } else {
                    let mut meta = Map::new();
                    if covered.contains(&(row, col)) {
                        meta.insert("docx_merge_continuation".into(), json!(true));
                    }
                    (String::new(), meta)
                };
                meta.extend([
                    ("table_id".into(), json!(id)),
                    ("row".into(), json!(row)),
                    ("col".into(), json!(col)),
                    ("rows".into(), json!(rows)),
                    ("cols".into(), json!(cols)),
                ]);
                chapter.push(text, "text", meta);
            }
        }
        Ok(true)
    }
}

fn shift<T>(
    spans: impl IntoIterator<Item = Span<T>>,
    offset: usize,
) -> impl Iterator<Item = Span<T>> {
    spans.into_iter().map(move |mut span| {
        span.source_start += offset;
        span.source_end += offset;
        span
    })
}
