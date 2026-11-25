//! Project version 1 of the manifest-ordered export input into Word blocks.
//! The caller supplies policy-adjusted snapshot text and saved metadata, not a
//! rendering plan. Only one chapter and one contiguous table are staged here.

use std::{borrow::Cow, collections::BTreeMap};

use anyhow::{anyhow, ensure, Context, Result};
use serde::{de::value::MapDeserializer, Deserialize};
use serde_json::Value;

use crate::alignment::target_digest;

use super::{
    export::{Cell, ListRef, Paragraph, Placement, Side, Table, Writer},
    properties::{Lists, ParagraphStyle, RunStyle},
};

#[derive(Deserialize)]
pub(super) struct Header {
    version: u8,
    options: Options,
    numbering: Lists,
    core_properties: BTreeMap<String, String>,
}

impl Header {
    pub fn start(self) -> Result<(Writer, Options)> {
        ensure!(self.version == 1, "Unsupported DOCX export input version");
        Ok((
            Writer::new(self.numbering, self.core_properties),
            self.options,
        ))
    }
}

#[derive(Deserialize)]
pub(super) struct Options {
    target_font: Option<String>,
    bilingual: bool,
    source_first: bool,
    preserve_source_style: bool,
}

#[derive(Deserialize)]
pub(super) struct Chapter {
    title: String,
    explicit_title: bool,
    #[serde(default)]
    heading_level: Value,
    segments: Vec<Segment>,
}

#[derive(Deserialize)]
struct Segment {
    source: String,
    target: Option<String>,
    #[serde(default)]
    cont: bool,
    #[serde(default)]
    heading: bool,
    #[serde(default)]
    meta: Value,
}

impl Segment {
    fn target(&self) -> &str {
        self.target.as_deref().unwrap_or(&self.source)
    }

    fn heading_level(&self, chapter: &Chapter) -> Option<u8> {
        self.heading.then(|| {
            self.meta
                .get("heading_level")
                .unwrap_or(&chapter.heading_level)
                .as_u64()
                .unwrap_or(1)
                .clamp(1, 9) as u8
        })
    }
}

impl Chapter {
    pub fn write(&self, writer: &mut Writer, options: &Options) -> Result<()> {
        if self.explicit_title
            && !self.title.is_empty()
            && !self
                .segments
                .iter()
                .any(|s| s.heading_level(self) == Some(1))
        {
            writer.paragraph(&Paragraph {
                heading: Some(1),
                sides: vec![Side {
                    text: self.title.clone(),
                    font: options.target_font.clone(),
                    ..Default::default()
                }],
                ..Default::default()
            });
        }

        let mut active_table: Option<(i64, Table)> = None;
        let mut segments = self.segments.as_slice();
        while let Some((first, tail)) = segments.split_first() {
            let count = 1 + tail
                .iter()
                .take_while(|s| s.cont && s.meta.get("table_id").is_none())
                .count();
            let group = &segments[..count];
            segments = &segments[count..];

            let meta = &first.meta;
            if meta.get("html_attribute").is_some() {
                continue;
            }
            let table_id = meta.get("table_id").and_then(integer);
            if let Some((_, table)) = active_table.take_if(|(id, _)| Some(*id) != table_id) {
                writer.table(table)?;
            }
            if let Some(id) = table_id {
                let (_, table) = active_table.get_or_insert_with(|| {
                    (
                        id,
                        Table {
                            rows: 1,
                            cols: 1,
                            cells: Vec::new(),
                        },
                    )
                });
                table.rows = table.rows.max(geometry(meta, "rows", 1, true)?);
                table.cols = table.cols.max(geometry(meta, "cols", 1, true)?);
                if !truthy(&meta["docx_merge_continuation"]) {
                    table.cells.push(Cell {
                        row: geometry(meta, "row", 0, true)?,
                        col: geometry(meta, "col", 0, true)?,
                        width: geometry(meta, "docx_grid_span", 1, false)?,
                        height: geometry(meta, "docx_row_span", 1, false)?,
                        paragraphs: paragraphs(group, None, true, options)?,
                    });
                }
            } else if group
                .iter()
                .any(|s| !blank(s.target()) || !blank(&s.source))
            {
                for paragraph in paragraphs(group, first.heading_level(self), false, options)? {
                    writer.paragraph(&paragraph);
                }
            }
        }
        if let Some((_, table)) = active_table {
            writer.table(table)?;
        }
        Ok(())
    }
}

fn paragraphs(
    group: &[Segment],
    heading: Option<u8>,
    in_table: bool,
    options: &Options,
) -> Result<Vec<Paragraph>> {
    let target: String = group.iter().map(Segment::target).collect();
    let source: String = group.iter().map(|s| s.source.as_str()).collect();
    let meta = &group[0].meta;
    let style = if meta.is_null() {
        ParagraphStyle::default()
    } else {
        ParagraphStyle::deserialize(meta).context("Invalid DOCX paragraph properties")?
    };
    let src = if options.bilingual && heading.is_none() {
        bilingual_source(&source, &target)
    } else {
        None
    };
    let font = if target != source && !blank(&target) {
        options.target_font.as_deref()
    } else {
        None
    };
    let target_side = side(target, &source, meta, font)?;
    let list = meta["list_num_id"]
        .as_i64()
        .filter(|&id| id > 0)
        .zip(meta["list_fmt"].as_str())
        .map(|(id, format)| ListRef {
            id,
            level: integer(&meta["list_ilvl"]).unwrap_or(0).clamp(0, 8) as u8,
            format: format.to_owned(),
            source: source.clone(),
        });
    let mut paragraph = Paragraph {
        style,
        heading,
        list,
        sides: vec![target_side],
    };

    // Headings remain single outline entries. Unnumbered cells use a line
    // break, but numbered cells need separate paragraphs to number only target.
    let Some(src) = src else {
        return Ok(vec![paragraph]);
    };
    let source_side = side(
        src.into_owned(),
        &source,
        if options.preserve_source_style {
            meta
        } else {
            &Value::Null
        },
        None,
    )?;
    if in_table && paragraph.list.is_none() {
        paragraph
            .sides
            .insert(usize::from(!options.source_first), source_side);
        return Ok(vec![paragraph]);
    }
    let source_paragraph = Paragraph {
        style: paragraph.style.clone(),
        sides: vec![source_side],
        ..Default::default()
    };
    Ok(if options.source_first {
        vec![source_paragraph, paragraph]
    } else {
        vec![paragraph, source_paragraph]
    })
}

fn side(text: String, source: &str, meta: &Value, font: Option<&str>) -> Result<Side> {
    let mut side = Side {
        font: font.map(str::to_owned),
        ..Default::default()
    };
    let runs = &meta["docx_source_runs"];
    if text == source && truthy(runs) {
        side.source_runs = Deserialize::deserialize(runs).context("Invalid DOCX source runs")?;
    }
    let uniform = &meta["docx_style"];
    if uniform.as_object().is_some_and(|style| !style.is_empty()) {
        side.style = Some(RunStyle::deserialize(uniform).context("Invalid DOCX uniform style")?);
    } else if let Some(styles) = meta["docx_styles"].as_object() {
        let saved = styles
            .get("placements")
            .and_then(Value::as_array)
            .filter(|rows| {
                !rows.is_empty()
                    && styles
                        .get("target_digest")
                        .and_then(Value::as_str)
                        .is_some_and(|digest| digest == target_digest(&text))
            });
        side.placements = if let Some(saved) = saved {
            // First saved placement wins on overlap (in Side::write). Keep the
            // original row order and do not fall back merely because a row is bad.
            saved
                .iter()
                .filter(|row| {
                    integer(&row["target_start"]).is_some() && integer(&row["target_end"]).is_some()
                })
                .map(Placement::deserialize)
                .collect::<Result<_, _>>()
                .context("Invalid saved DOCX style placement")?
        } else if let Some(items) = styles.get("items").and_then(Value::as_array) {
            proportional(source, &text, items).context("Invalid DOCX fallback style")?
        } else {
            Vec::new()
        };
    }
    side.text = text;
    Ok(side)
}

fn proportional(source: &str, target: &str, items: &[Value]) -> Result<Vec<Placement>> {
    // Match Python proportional_range_placements: positions count code points
    // (not bytes or UTF-16), round half up, and use floor division for negatives.
    // Clamp only the upper bound here; the renderer clamps both to the run.
    let ns = source.chars().count() as i128;
    let nt = target.chars().count() as i128;
    let project = |n| {
        if ns == 0 {
            0
        } else {
            ((i128::from(n) * nt + ns / 2).div_euclid(ns)).clamp(i128::from(i64::MIN), nt) as i64
        }
    };
    items
        .iter()
        .filter_map(|item| {
            let _ = item["id"].as_str().filter(|s| !s.is_empty())?;
            Some((
                item,
                project(integer(&item["source_start"])?),
                project(integer(&item["source_end"])?),
            ))
        })
        .map(|(item, start, end)| {
            // Inherit only the properties exported by Python style_fields.
            let fields = ["bold", "italic", "underline", "color", "size_pt"];
            let style = RunStyle::deserialize(MapDeserializer::new(
                fields.iter().filter_map(|&key| Some((key, item.get(key)?))),
            ))?;
            Ok(Placement {
                target_start: start,
                target_end: end.max(start),
                style,
            })
        })
        .collect()
}

// These predicates mirror the saved-state conventions of the Python planner.
fn blank(text: &str) -> bool {
    text.chars()
        .all(|c| c.is_whitespace() || matches!(c, '\x1c'..='\x1f'))
}

fn integer(value: &Value) -> Option<i64> {
    value.as_i64().or_else(|| value.as_bool().map(i64::from))
}

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

fn geometry(meta: &Value, field: &str, default: usize, clamp_zero: bool) -> Result<usize> {
    let value = &meta[field];
    if !truthy(value) {
        return Ok(default);
    }
    let converted = integer(value)
        .or_else(|| value.as_f64().map(|n| n as i64))
        .or_else(|| value.as_str()?.trim().parse().ok())
        .ok_or_else(|| anyhow!("Invalid DOCX table {field}"))?;
    Ok(usize::try_from(if clamp_zero {
        converted.max(0)
    } else {
        converted
    })?)
}

fn bilingual_source<'a>(source: &'a str, target: &str) -> Option<Cow<'a, str>> {
    if source == target || blank(target) {
        return None;
    }
    // Strip pronunciation hints as Python does, reserving the "object:" prefix.
    let mut rest = source;
    let mut last = 0;
    let mut out = String::new();
    while let Some((prefix, tail)) = rest.split_once('〘') {
        if tail.starts_with("object:") {
            rest = tail;
            continue;
        }
        let Some((_, next)) = tail.split_once('〙') else {
            break;
        };
        let start = source.len() - rest.len() + prefix.len();
        out.push_str(&source[last..start]);
        rest = next;
        last = source.len() - rest.len();
    }
    let plain = if last == 0 {
        Cow::Borrowed(source)
    } else {
        out.push_str(&source[last..]);
        Cow::Owned(out)
    };
    (!blank(&plain) && plain != target).then_some(plain)
}
