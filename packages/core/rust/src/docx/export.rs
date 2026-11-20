use std::{
    collections::{BTreeMap, BTreeSet},
    fmt::Write,
    fs::File,
    io::{BufWriter, Write as _},
    path::Path,
};

use anyhow::{ensure, Result};
use serde::Deserialize;
use zip::{write::SimpleFileOptions, CompressionMethod, ZipWriter};

use super::{
    has_list_prefix,
    opc::TRANSITIONAL,
    properties::{
        element, rgb, text, value, Attributes, ListLevel, Lists, ParagraphStyle, RunStyle,
        SourceRun, Span, CORE_PROPERTIES, FONTS, PARAGRAPH,
    },
};

const XML: &str = r#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?>"#;
const OFFICE_REL: &str = "http://schemas.openxmlformats.org/officeDocument/2006/relationships";
const PACKAGE_REL: &str = "http://schemas.openxmlformats.org/package/2006/relationships";
const WIDTH: usize = 8640; // US Letter with 1.25" margins, as in the previous template.

#[derive(Default)]
pub(super) struct Paragraph {
    pub style: ParagraphStyle,
    pub heading: Option<u8>,
    pub list: Option<ListRef>,
    pub sides: Vec<Side>,
}

pub(super) struct ListRef {
    pub id: i64,
    pub level: u8,
    pub format: String,
    pub source: String,
}

#[derive(Default)]
pub(super) struct Side {
    pub text: String,
    pub font: Option<String>,
    pub style: Option<RunStyle>,
    pub placements: Vec<Placement>,
    pub source_runs: Vec<Span<SourceRun>>,
}

#[derive(Deserialize)]
pub(super) struct Placement {
    pub target_start: i64,
    pub target_end: i64,
    #[serde(flatten)]
    pub style: RunStyle,
}

pub(super) struct Table {
    pub rows: usize,
    pub cols: usize,
    pub cells: Vec<Cell>,
}

pub(super) struct Cell {
    pub row: usize,
    pub col: usize,
    pub width: usize,
    pub height: usize,
    pub paragraphs: Vec<Paragraph>,
}

pub(super) struct Writer {
    numbering: Numbering,
    body: String,
    core_properties: BTreeMap<String, String>,
}

impl Writer {
    pub fn new(numbering: Lists, core_properties: BTreeMap<String, String>) -> Self {
        let mut body = root("document");
        body.push_str("<w:body>");
        Self {
            numbering: Numbering::new(numbering),
            body,
            core_properties,
        }
    }

    pub fn paragraph(&mut self, plan: &Paragraph) {
        paragraph(&mut self.body, plan, &mut self.numbering);
    }

    pub fn table(&mut self, plan: Table) -> Result<()> {
        table(&mut self.body, plan, &mut self.numbering)
    }

    pub fn finish(mut self, path: &Path) -> Result<()> {
        self.body.push_str(concat!(
            "<w:sectPr><w:pgSz w:w=\"12240\" w:h=\"15840\"/>",
            "<w:pgMar w:top=\"1440\" w:right=\"1800\" w:bottom=\"1440\" w:left=\"1800\"",
            " w:header=\"720\" w:footer=\"720\" w:gutter=\"0\"/></w:sectPr></w:body></w:document>"
        ));
        let core = core_properties(&self.core_properties);
        let mut zip = ZipWriter::new(BufWriter::new(File::create(path)?));
        let options = SimpleFileOptions::default()
            .compression_method(CompressionMethod::Deflated)
            .compression_level(Some(3));
        for (name, data) in [
            ("[Content_Types].xml", content_types()),
            ("_rels/.rels", relationships(false)),
            ("word/_rels/document.xml.rels", relationships(true)),
            ("word/styles.xml", styles()),
            ("word/numbering.xml", self.numbering.xml()),
            ("word/document.xml", self.body),
            ("docProps/core.xml", core),
        ] {
            zip.start_file(name, options)?;
            zip.write_all(data.as_bytes())?;
        }
        zip.finish()?.flush()?;
        Ok(())
    }
}

fn root(name: &str) -> String {
    format!("{XML}<w:{name} xmlns:w=\"{TRANSITIONAL}\">")
}

fn content_types() -> String {
    format!(concat!(
        "{XML}<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\">",
        "<Default Extension=\"rels\" ContentType=\"application/vnd.openxmlformats-package.relationships+xml\"/>",
        "<Default Extension=\"xml\" ContentType=\"application/xml\"/>",
        "<Override PartName=\"/word/document.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml\"/>",
        "<Override PartName=\"/word/styles.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml\"/>",
        "<Override PartName=\"/word/numbering.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml\"/>",
        "<Override PartName=\"/docProps/core.xml\" ContentType=\"application/vnd.openxmlformats-package.core-properties+xml\"/>",
        "</Types>"
    ), XML = XML)
}

fn relationships(word: bool) -> String {
    let mut xml = format!("{XML}<Relationships xmlns=\"{PACKAGE_REL}\">");
    if word {
        for (i, part) in ["styles", "numbering"].into_iter().enumerate() {
            write!(
                xml,
                "<Relationship Id=\"rId{}\" Type=\"{OFFICE_REL}/{part}\" Target=\"{part}.xml\"/>",
                i + 1
            )
            .unwrap();
        }
    } else {
        write!(xml, concat!(
            "<Relationship Id=\"rId1\" Type=\"{OFFICE_REL}/officeDocument\" Target=\"word/document.xml\"/>",
            "<Relationship Id=\"rId2\" Type=\"{PACKAGE_REL}/metadata/core-properties\" Target=\"docProps/core.xml\"/>"
        ), OFFICE_REL = OFFICE_REL, PACKAGE_REL = PACKAGE_REL).unwrap();
    }
    xml.push_str("</Relationships>");
    xml
}

fn core_properties(values: &BTreeMap<String, String>) -> String {
    let mut xml = format!(
        "{XML}<cp:coreProperties xmlns:cp=\"http://schemas.openxmlformats.org/package/2006/metadata/core-properties\" \
        xmlns:dc=\"http://purl.org/dc/elements/1.1/\" xmlns:dcterms=\"http://purl.org/dc/terms/\" \
        xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\">"
    );
    for &(prefix, name, _) in CORE_PROPERTIES {
        if let Some(content) = values.get(name) {
            let datatype = if prefix == "dcterms" {
                r#" xsi:type="dcterms:W3CDTF""#
            } else {
                ""
            };
            write!(xml, "<{prefix}:{name}{datatype}>").unwrap();
            text(&mut xml, content);
            write!(xml, "</{prefix}:{name}>").unwrap();
        }
    }
    xml.push_str("</cp:coreProperties>");
    xml
}

fn styles() -> String {
    let mut xml = root("styles");
    xml.push_str(concat!(
        "<w:docDefaults><w:rPrDefault><w:rPr><w:sz w:val=\"22\"/><w:szCs w:val=\"22\"/>",
        "</w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:after=\"200\"",
        " w:line=\"276\" w:lineRule=\"auto\"/></w:pPr></w:pPrDefault></w:docDefaults>",
        "<w:style w:type=\"paragraph\" w:default=\"1\" w:styleId=\"Normal\">",
        "<w:name w:val=\"Normal\"/><w:qFormat/></w:style>"
    ));
    for level in 1..=9 {
        write!(xml, concat!(
            "<w:style w:type=\"paragraph\" w:styleId=\"Heading{level}\">",
            "<w:name w:val=\"heading {level}\"/><w:basedOn w:val=\"Normal\"/><w:next w:val=\"Normal\"/>",
            "<w:uiPriority w:val=\"9\"/><w:qFormat/><w:pPr><w:keepNext/><w:keepLines/>",
            "<w:spacing w:before=\"240\" w:after=\"0\"/><w:outlineLvl w:val=\"{}\"/>",
            "</w:pPr><w:rPr><w:b/><w:sz w:val=\"{}\"/></w:rPr></w:style>"
        ), level - 1, (34 - level * 2).max(22), level = level).unwrap();
        write!(
            xml,
            concat!(
                "<w:style w:type=\"paragraph\" w:styleId=\"TOC{level}\">",
                "<w:name w:val=\"toc {level}\"/><w:basedOn w:val=\"Normal\"/><w:pPr>",
                "<w:tabs><w:tab w:val=\"right\" w:leader=\"dot\" w:pos=\"{WIDTH}\"/></w:tabs>",
                "<w:spacing w:after=\"100\"/><w:ind w:left=\"{}\"/><w:outlineLvl w:val=\"9\"/>",
                "</w:pPr></w:style>"
            ),
            (level - 1) * 240,
            level = level,
            WIDTH = WIDTH
        )
        .unwrap();
    }
    for name in ["Number", "Bullet"] {
        for level in 1..=3 {
            let (suffix, label) = if level == 1 {
                (String::new(), String::new())
            } else {
                (level.to_string(), format!(" {level}"))
            };
            write!(
                xml,
                concat!(
                    "<w:style w:type=\"paragraph\" w:styleId=\"List{name}{suffix}\">",
                    "<w:name w:val=\"List {name}{label}\"/><w:basedOn w:val=\"Normal\"/>",
                    "<w:pPr><w:contextualSpacing/></w:pPr></w:style>"
                ),
                name = name,
                suffix = suffix,
                label = label
            )
            .unwrap();
        }
    }
    xml.push_str(concat!(
        "<w:style w:type=\"table\" w:styleId=\"TableGrid\"><w:name w:val=\"Table Grid\"/>",
        "<w:tblPr><w:tblBorders>"
    ));
    for side in ["top", "left", "bottom", "right", "insideH", "insideV"] {
        write!(
            xml,
            "<w:{side} w:val=\"single\" w:sz=\"4\" w:color=\"auto\"/>"
        )
        .unwrap();
    }
    xml.push_str("</w:tblBorders></w:tblPr></w:style></w:styles>");
    xml
}

struct Numbering {
    definitions: Lists,
    used: BTreeMap<i64, (usize, BTreeMap<u8, String>)>,
}

impl Numbering {
    fn new(definitions: Lists) -> Self {
        Self {
            definitions,
            used: BTreeMap::new(),
        }
    }

    fn id(&mut self, list: &ListRef) -> usize {
        let next = self.used.len() + 1;
        let (id, levels) = self
            .used
            .entry(list.id)
            .or_insert_with(|| (next, BTreeMap::new()));
        levels
            .entry(list.level.min(8))
            .or_insert_with(|| list.format.clone());
        *id
    }

    fn xml(mut self) -> String {
        let mut xml = root("numbering");
        for (original, (id, used)) in &self.used {
            let levels = self.definitions.entry(*original).or_default();
            for (&index, fmt) in used {
                levels
                    .entry(index)
                    .or_insert_with(|| fallback_level(index, fmt));
            }
            let format = if levels.keys().any(|&idx| idx > 0) {
                "multilevel"
            } else {
                "singleLevel"
            };
            write!(xml, "<w:abstractNum w:abstractNumId=\"{id}\">").unwrap();
            value(&mut xml, "multiLevelType", format);
            let last = levels.keys().copied().filter(|&i| i < 9).max().unwrap_or(0);
            for index in 0..=last {
                let fallback;
                let level = if let Some(level) = levels.get(&index) {
                    level
                } else {
                    fallback = fallback_level(index, "decimal");
                    &fallback
                };
                write_level(&mut xml, index, level);
            }
            xml.push_str("</w:abstractNum>");
        }
        for (original, (id, _)) in &self.used {
            write!(xml, "<w:num w:numId=\"{id}\">").unwrap();
            value(&mut xml, "abstractNumId", id.to_string());
            for (&index, level) in &self.definitions[original] {
                if let Some(start) = level.start_override.filter(|_| index < 9) {
                    write!(xml, "<w:lvlOverride w:ilvl=\"{index}\">").unwrap();
                    value(&mut xml, "startOverride", start.to_string());
                    xml.push_str("</w:lvlOverride>");
                }
            }
            xml.push_str("</w:num>");
        }
        xml.push_str("</w:numbering>");
        xml
    }
}

fn fallback_level(level: u8, fmt: &str) -> ListLevel {
    let mut result = ListLevel::default();
    let marker = match fmt {
        "bullet" => "•".to_owned(),
        "none" => String::new(),
        _ => format!("%{}.", level + 1),
    };
    result.spec = [
        ("start", "1".into()),
        ("numFmt", fmt.into()),
        ("lvlText", marker),
        ("lvlJc", "left".into()),
    ]
    .into_iter()
    .map(|(k, v)| (k.into(), v))
    .collect();
    result.paragraph.docx_paragraph.insert(
        "ind".into(),
        [
            ("left".into(), ((usize::from(level) + 1) * 360).to_string()),
            ("hanging".into(), "360".into()),
        ]
        .into_iter()
        .collect(),
    );
    result
}

fn write_level(out: &mut String, index: u8, level: &ListLevel) {
    write!(out, "<w:lvl w:ilvl=\"{index}\">").unwrap();
    let fallback = fallback_level(index, "decimal");
    for key in [
        "start",
        "numFmt",
        "lvlRestart",
        "isLgl",
        "suff",
        "lvlText",
        "lvlJc",
    ] {
        let prop = if key == "isLgl" {
            level.legal.as_ref()
        } else {
            level.spec.get(key).or_else(|| fallback.spec.get(key))
        };
        if let Some(val) = prop {
            value(out, key, val);
        }
    }
    out.push_str("<w:pPr>");
    paragraph_props(out, &level.paragraph);
    out.push_str("</w:pPr>");
    run_props(out, Some(&level.run), None, Some(&level.fonts), None);
    out.push_str("</w:lvl>");
}

fn paragraph(out: &mut String, plan: &Paragraph, numbering: &mut Numbering) {
    out.push_str("<w:p><w:pPr>");
    let heading = plan.heading.map(|n| n.clamp(1, 9));
    let list = plan.list.as_ref().filter(|list| {
        list.id > 0
            && !has_list_prefix(&list.source)
            && !plan
                .sides
                .first()
                .is_some_and(|side| has_list_prefix(&side.text))
    });
    if let Some(level) = heading {
        value(out, "pStyle", format!("Heading{level}"));
    } else if let Some(toc) = plan.style.docx_toc_level.filter(|&n| (1..=9).contains(&n)) {
        value(out, "pStyle", format!("TOC{toc}"));
    } else if let Some(list) = list {
        let name = if matches!(list.format.as_str(), "bullet" | "none") {
            "ListBullet"
        } else {
            "ListNumber"
        };
        let level = list.level.min(2);
        let style = if level == 0 {
            name.into()
        } else {
            format!("{name}{}", level + 1)
        };
        value(out, "pStyle", style);
    }
    // numPr sits after keep/line controls, but before shading, tabs and indentation.
    simple_props(out, &plan.style, &PARAGRAPH[..4]);
    if let Some(list) = list {
        out.push_str("<w:numPr>");
        value(out, "ilvl", list.level.min(8).to_string());
        value(out, "numId", numbering.id(list).to_string());
        out.push_str("</w:numPr>");
    }
    paragraph_props(out, &plan.style);
    if let Some(level) = heading {
        value(out, "outlineLvl", (level - 1).to_string());
    }
    out.push_str("</w:pPr>");
    for (idx, side) in plan.sides.iter().enumerate() {
        if idx > 0 {
            write_run(out, "\n", None, None, None, false);
        }
        side.write(out, heading.is_some());
    }
    out.push_str("</w:p>");
}

fn simple_props(out: &mut String, style: &ParagraphStyle, defs: &[(&str, &[&str])]) {
    for &(tag, attrs) in defs {
        if let Some(value) = style.docx_paragraph.get(tag) {
            element(out, tag, value, attrs);
        }
    }
}

fn paragraph_props(out: &mut String, style: &ParagraphStyle) {
    if let Some(shade) = style.shade.as_deref().and_then(rgb) {
        write!(
            out,
            "<w:shd w:val=\"clear\" w:color=\"auto\" w:fill=\"{shade}\"/>"
        )
        .unwrap();
    }
    if !style.docx_tabs.is_empty() {
        out.push_str("<w:tabs>");
        for tab in &style.docx_tabs {
            element(out, "tab", tab, &["val", "leader", "pos"]);
        }
        out.push_str("</w:tabs>");
    }
    simple_props(out, style, &PARAGRAPH[4..]);
    if let Some(align) = style.align.as_deref() {
        let align = match align.trim() {
            "justify" => "both",
            "left" | "right" | "center" | "both" | "distribute" | "mediumKashida" | "numTab"
            | "highKashida" | "lowKashida" | "thaiDistribute" => align.trim(),
            _ => return,
        };
        value(out, "jc", align);
    }
}

impl Side {
    fn write(&self, out: &mut String, heading: bool) {
        if self.text.is_empty() {
            return;
        }
        if self.placements.is_empty() && self.source_runs.is_empty() {
            write_run(
                out,
                &self.text,
                self.style.as_ref(),
                self.font.as_deref(),
                None,
                heading,
            );
            return;
        }
        // Saved offsets are Unicode scalar indices. Resolve them once and sweep the
        // events in order. For overlapping placements the first saved row wins.
        let mut offsets: Vec<_> = self.text.char_indices().map(|(i, _)| i).collect();
        offsets.push(self.text.len());
        let len = offsets.len() - 1;
        let mut events = Vec::new();
        let mut add = |start: usize, end: usize, source: bool, i| {
            if start < end {
                events.extend([(start, true, source, i), (end, false, source, i)]);
            }
        };
        let clamp = |n: i64| n.clamp(0, len as i64) as usize;
        for (i, pos) in self.placements.iter().enumerate() {
            if !pos.style.empty() {
                add(clamp(pos.target_start), clamp(pos.target_end), false, i);
            }
        }
        for (i, pos) in self.source_runs.iter().enumerate() {
            add(pos.source_start.min(len), pos.source_end.min(len), true, i);
        }
        events.sort_unstable();
        let mut start = 0;
        let mut styles: BTreeSet<usize> = BTreeSet::new();
        let mut sources: BTreeSet<usize> = BTreeSet::new();
        for (end, open, source, i) in events.into_iter().chain([(len, false, false, usize::MAX)]) {
            if start < end {
                let style = styles
                    .first()
                    .map(|&i| &self.placements[i].style)
                    .or(self.style.as_ref());
                let format = sources.first().map(|&i| &self.source_runs[i].style);
                write_run(
                    out,
                    &self.text[offsets[start]..offsets[end]],
                    style,
                    self.font.as_deref(),
                    format,
                    heading,
                );
            }
            let active = if source { &mut sources } else { &mut styles };
            if open {
                active.insert(i);
            } else {
                active.remove(&i);
            }
            start = end;
        }
    }
}

fn run_props(
    out: &mut String,
    style: Option<&RunStyle>,
    font: Option<&str>,
    source_fonts: Option<&Attributes>,
    overrides: Option<(Option<f64>, bool)>,
) {
    let (source_size, heading) = overrides.unwrap_or_default();
    let font = font
        .or_else(|| style?.font.as_deref())
        .map(str::trim)
        .filter(|s| !s.is_empty());
    let mut fonts = source_fonts.filter(|fonts| !fonts.is_empty());
    let uniform;
    if let Some(font) = font {
        uniform = ["ascii", "hAnsi", "eastAsia", "cs"]
            .into_iter()
            .map(|key| (key.into(), font.into()))
            .collect();
        fonts = Some(&uniform);
    }
    let color = style.and_then(|s| s.color.as_deref()).and_then(rgb);
    let size = source_size
        .or(style.and_then(|s| s.size_pt))
        .filter(|size| (0.5..=1638.).contains(size));
    if !heading && fonts.is_none() && size.is_none() && style.is_none_or(RunStyle::empty) {
        return;
    }
    out.push_str("<w:rPr>");
    if let Some(fonts) = fonts {
        element(out, "rFonts", fonts, FONTS);
    }
    for (tag, toggle) in [
        ("b", style.and_then(|s| s.bold)),
        ("i", style.and_then(|s| s.italic)),
    ] {
        if let Some(toggle) = toggle {
            value(out, tag, if toggle { "1" } else { "0" });
        }
    }
    if let Some(color) = color.as_deref().or(heading.then_some("000000")) {
        value(out, "color", color);
    }
    if let Some(size) = size {
        value(out, "sz", ((size * 2.) as u32).to_string());
    }
    if let Some(underline) = style.and_then(|s| s.underline) {
        value(out, "u", if underline { "single" } else { "none" });
    }
    out.push_str("</w:rPr>");
}

fn write_run(
    out: &mut String,
    value: &str,
    style: Option<&RunStyle>,
    font: Option<&str>,
    source: Option<&SourceRun>,
    heading: bool,
) {
    out.push_str("<w:r>");
    run_props(
        out,
        style,
        font,
        source.map(|s| &s.fonts),
        Some((source.and_then(|s| s.size_pt), heading)),
    );
    let mut start = 0;
    let mut it = value.char_indices().peekable();
    while let Some((pos, c)) = it.next() {
        if !matches!(c, '\r' | '\n' | '\t') {
            continue;
        }
        text_node(out, &value[start..pos]);
        out.push_str(if c == '\t' { "<w:tab/>" } else { "<w:br/>" });
        start = pos + 1;
        if c == '\r' && it.peek().is_some_and(|&(_, next)| next == '\n') {
            it.next();
            start += 1;
        }
    }
    text_node(out, &value[start..]);
    out.push_str("</w:r>");
}

fn text_node(out: &mut String, fragment: &str) {
    if !fragment.is_empty() {
        out.push_str("<w:t xml:space=\"preserve\">");
        text(out, fragment);
        out.push_str("</w:t>");
    }
}

fn table(out: &mut String, plan: Table, numbering: &mut Numbering) -> Result<()> {
    ensure!(
        plan.rows > 0 && plan.cols > 0 && plan.rows.saturating_mul(plan.cols) <= 1_000_000,
        "DOCX table grid is invalid"
    );
    let mut grid = vec![None; plan.rows * plan.cols];
    for (i, cell) in plan.cells.iter().enumerate() {
        if cell.row >= plan.rows || cell.col >= plan.cols {
            continue;
        }
        let height = cell.height.clamp(1, plan.rows - cell.row);
        let width = cell.width.clamp(1, plan.cols - cell.col);
        for r in cell.row..cell.row + height {
            for c in cell.col..cell.col + width {
                let slot = &mut grid[r * plan.cols + c];
                ensure!(slot.is_none(), "Overlapping DOCX table cells");
                *slot = Some(i);
            }
        }
    }
    out.push_str(concat!(
        "<w:tbl><w:tblPr><w:tblStyle w:val=\"TableGrid\"/><w:tblW w:type=\"auto\" w:w=\"0\"/>",
        "<w:tblLook w:val=\"04A0\"/></w:tblPr><w:tblGrid>"
    ));
    let width = (WIDTH / plan.cols).max(1);
    for _ in 0..plan.cols {
        write!(out, "<w:gridCol w:w=\"{width}\"/>").unwrap();
    }
    out.push_str("</w:tblGrid>");
    for row in 0..plan.rows {
        out.push_str("<w:tr>");
        for col in 0..plan.cols {
            let cell = grid[row * plan.cols + col].map(|i| &plan.cells[i]);
            if cell.is_some_and(|c| c.col != col) {
                continue;
            }
            let span = cell.map_or(1, |cell| cell.width.clamp(1, plan.cols - col));
            write!(
                out,
                "<w:tc><w:tcPr><w:tcW w:type=\"dxa\" w:w=\"{}\"/>",
                width * span
            )
            .unwrap();
            if span > 1 {
                value(out, "gridSpan", span.to_string());
            }
            if cell.is_some_and(|cell| cell.height > 1 && cell.row + 1 < plan.rows) {
                value(
                    out,
                    "vMerge",
                    if cell.is_some_and(|cell| cell.row == row) {
                        "restart"
                    } else {
                        "continue"
                    },
                );
            }
            out.push_str("</w:tcPr>");
            if let Some(cell) = cell.filter(|cell| cell.row == row && !cell.paragraphs.is_empty()) {
                for block in &cell.paragraphs {
                    paragraph(out, block, numbering);
                }
            } else {
                out.push_str("<w:p/>");
            }
            out.push_str("</w:tc>");
        }
        out.push_str("</w:tr>");
    }
    out.push_str("</w:tbl>");
    Ok(())
}
