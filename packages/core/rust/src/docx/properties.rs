//! Shared, allowlisted Word properties for persisted state. Never re-emit raw source XML.

use std::{collections::BTreeMap, fmt::Write};

use quick_xml::escape::{escape, partial_escape};
use roxmltree::Node;
use serde::{Deserialize, Serialize};

use super::opc::{STRICT, TRANSITIONAL};

pub(super) type Word<'a, 'input> = Node<'a, 'input>;
pub(super) type Attributes = BTreeMap<String, String>;
pub(super) type Props = BTreeMap<String, Attributes>;

// Simple properties that carry no part relationships. Ordering is the OOXML schema order.
pub(super) const PARAGRAPH: &[(&str, &[&str])] = &[
    ("keepNext", &["val"]),
    ("keepLines", &["val"]),
    ("pageBreakBefore", &["val"]),
    ("widowControl", &["val"]),
    ("bidi", &["val"]),
    (
        "spacing",
        &[
            "before",
            "after",
            "beforeLines",
            "afterLines",
            "beforeAutospacing",
            "afterAutospacing",
            "line",
            "lineRule",
        ],
    ),
    (
        "ind",
        &[
            "left",
            "right",
            "start",
            "end",
            "firstLine",
            "hanging",
            "leftChars",
            "rightChars",
            "startChars",
            "endChars",
            "firstLineChars",
            "hangingChars",
        ],
    ),
    ("contextualSpacing", &["val"]),
];
pub(super) const FONTS: &[&str] = &["ascii", "hAnsi", "eastAsia", "cs", "hint"];

const DC: &str = "http://purl.org/dc/elements/1.1/";
const TERMS: &str = "http://purl.org/dc/terms/";
const CP: &str = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties";
pub(super) const CORE_PROPERTIES: &[(&str, &str, &str)] = &[
    ("dc", "title", DC),
    ("dc", "subject", DC),
    ("dc", "creator", DC),
    ("dc", "description", DC),
    ("dc", "identifier", DC),
    ("dc", "language", DC),
    ("cp", "keywords", CP),
    ("cp", "lastModifiedBy", CP),
    ("cp", "category", CP),
    ("cp", "contentStatus", CP),
    ("cp", "version", CP),
    ("cp", "revision", CP),
    ("dcterms", "created", TERMS),
    ("dcterms", "modified", TERMS),
];

pub(super) fn ns<'a>(node: Word<'a, '_>) -> Option<&'a str> {
    node.tag_name()
        .namespace()
        .filter(|v| matches!(*v, TRANSITIONAL | STRICT))
}

pub(super) fn is(node: Word<'_, '_>, tag: &str) -> bool {
    ns(node).is_some() && node.tag_name().name() == tag
}

pub(super) fn child<'a, 'input>(node: Word<'a, 'input>, tag: &str) -> Option<Word<'a, 'input>> {
    node.children().find(|&n| is(n, tag))
}

pub(super) fn attr<'a, 'input>(node: Word<'a, 'input>, name: &str) -> Option<&'a str> {
    node.attribute((ns(node)?, name))
}

pub(super) fn val<'a, 'input>(node: Word<'a, 'input>, tag: &str) -> Option<&'a str> {
    attr(child(node, tag)?, "val")
}

pub(super) fn attrs(node: Word<'_, '_>, names: &[&str]) -> Attributes {
    names
        .iter()
        .filter_map(|&name| Some((name.to_owned(), attr(node, name)?.to_owned())))
        .collect()
}

pub(super) fn value(out: &mut String, tag: &str, val: impl AsRef<str>) {
    write!(out, "<w:{tag} w:val=\"{}\"/>", escape(val.as_ref())).unwrap();
}

pub(super) fn element(out: &mut String, tag: &str, values: &Attributes, allow: &[&str]) {
    write!(out, "<w:{tag}").unwrap();
    for &name in allow {
        if let Some(val) = values.get(name) {
            write!(out, " w:{name}=\"{}\"", escape(val)).unwrap();
        }
    }
    out.push_str("/>");
}

pub(super) fn text(out: &mut String, value: &str) {
    // Invalid XML 1.0 control characters cannot be represented in Word text.
    if value.chars().any(invalid_xml_char) {
        out.push_str(&partial_escape(value.replace(invalid_xml_char, "\u{fffd}")));
    } else {
        out.push_str(&partial_escape(value));
    }
}

fn invalid_xml_char(c: char) -> bool {
    matches!(c, '\0'..='\u{8}' | '\u{b}'..='\u{c}' | '\u{e}'..='\u{1f}' | '\u{fffe}' | '\u{ffff}')
}

#[derive(Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub(super) struct RunStyle {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub bold: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub italic: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub underline: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub color: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub size_pt: Option<f64>,
    // `font` was accepted by the old writer-side slicing interface.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub font: Option<String>,
}

impl RunStyle {
    pub(super) fn apply(&mut self, node: Word<'_, '_>) {
        fn toggle(node: Word<'_, '_>, name: &str) -> Option<bool> {
            let value = attr(child(node, name)?, "val").unwrap_or("true");
            Some(!matches!(value, "0" | "false" | "off" | "none"))
        }
        self.bold = toggle(node, "b").or(self.bold);
        self.italic = toggle(node, "i").or(self.italic);
        self.underline = toggle(node, "u").or(self.underline);
        if let Some(value) = val(node, "color") {
            self.color = rgb(value);
        }
        if let Some(size) = val(node, "sz").and_then(|s| s.parse::<u32>().ok()) {
            self.size_pt = Some(f64::from(size) / 2.);
        }
    }

    pub(super) fn same_visible(&self, other: &Self) -> bool {
        self.bold == other.bold
            && self.italic == other.italic
            && self.underline == other.underline
            && self.color == other.color
    }

    pub(super) fn visible(&self) -> bool {
        !self.same_visible(&Self::default())
    }

    pub(super) fn empty(&self) -> bool {
        self == &Self::default()
    }
}

pub(super) fn rgb(raw: &str) -> Option<String> {
    let tail = raw.get(raw.len().checked_sub(6)?..)?;
    tail.bytes()
        .all(|c| c.is_ascii_hexdigit())
        .then(|| tail.to_ascii_uppercase())
}

#[derive(Clone, Serialize, Deserialize)]
pub(super) struct Span<T> {
    pub source_start: usize,
    pub source_end: usize,
    #[serde(flatten)]
    pub style: T,
}

#[derive(Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub(super) struct SourceRun {
    #[serde(skip_serializing_if = "BTreeMap::is_empty")]
    pub fonts: Attributes,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub size_pt: Option<f64>,
}

impl SourceRun {
    pub(super) fn empty(&self) -> bool {
        self.fonts.is_empty() && self.size_pt.is_none()
    }
}

#[derive(Clone, Default, Serialize, Deserialize)]
#[serde(default)]
pub(super) struct ParagraphStyle {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub align: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub shade: Option<String>,
    #[serde(skip_serializing_if = "BTreeMap::is_empty")]
    pub docx_paragraph: Props,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub docx_tabs: Vec<Attributes>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub docx_toc_level: Option<u8>,
}

impl ParagraphStyle {
    pub(super) fn apply(&mut self, node: Word<'_, '_>) {
        if let Some(align) = val(node, "jc") {
            let align = match align {
                "start" => "left",
                "end" => "right",
                "justify" => "both",
                other => other,
            };
            self.align = Some(align.to_owned());
        }
        if let Some(shade) = child(node, "shd").and_then(|n| attr(n, "fill")) {
            self.shade = rgb(shade);
        }
        for &(key, names) in PARAGRAPH {
            if let Some(prop) = child(node, key) {
                self.docx_paragraph
                    .entry(key.into())
                    .or_default()
                    .extend(attrs(prop, names));
            }
        }
        if let Some(tabs) = child(node, "tabs") {
            for tab in tabs.children().filter(|&n| is(n, "tab")) {
                let Some(position) = attr(tab, "pos") else {
                    continue;
                };
                self.docx_tabs
                    .retain(|tab| tab.get("pos").map(String::as_str) != Some(position));
                if attr(tab, "val") != Some("clear") {
                    self.docx_tabs.push(attrs(tab, &["val", "leader", "pos"]));
                }
            }
        }
    }
}

#[derive(Serialize, Deserialize, Default, Clone)]
#[serde(default)]
pub(super) struct ListLevel {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub start_override: Option<i64>,
    #[serde(skip_serializing_if = "BTreeMap::is_empty")]
    pub spec: BTreeMap<String, String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub legal: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub p_style: Option<String>,
    pub paragraph: ParagraphStyle,
    #[serde(skip_serializing_if = "RunStyle::empty")]
    pub run: RunStyle,
    #[serde(skip_serializing_if = "BTreeMap::is_empty")]
    pub fonts: Attributes,
}

pub(super) const LIST_PROPS: &[&str] =
    &["start", "numFmt", "lvlRestart", "suff", "lvlText", "lvlJc"];
pub(super) type Levels = BTreeMap<u8, ListLevel>;
pub(super) type Lists = BTreeMap<i64, Levels>;

impl ListLevel {
    pub(super) fn apply(&mut self, level: Word<'_, '_>) {
        for &name in LIST_PROPS {
            if let Some(value) = val(level, name) {
                self.spec.insert(name.to_owned(), value.to_owned());
            }
        }
        if let Some(legal) = child(level, "isLgl") {
            self.legal = Some(attr(legal, "val").unwrap_or("1").to_owned());
        }
        if let Some(style) = val(level, "pStyle") {
            self.p_style = Some(style.to_owned());
        }
        if let Some(node) = child(level, "pPr") {
            self.paragraph.apply(node);
        }
        if let Some(node) = child(level, "rPr") {
            self.run.apply(node);
            if let Some(fonts) = child(node, "rFonts") {
                self.fonts.extend(attrs(fonts, FONTS));
            }
        }
    }
}
