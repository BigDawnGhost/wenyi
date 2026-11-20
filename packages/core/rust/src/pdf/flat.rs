//! Merge page content without wrapping it in forms: some text extractors only
//! look for text operators in the page stream.

use std::{collections::HashMap, io::Write, ops::Range};

use anyhow::{bail, ensure, Context, Result};
use lopdf::{Dictionary, Document, Object, ObjectId, Stream};

use super::{geometry::Placement, inherited, CopyObjects};

#[derive(Default)]
pub(super) struct Spread {
    pub(super) resources: Dictionary,
    pub(super) operations: Vec<u8>,
    next_name: usize,
}

type Rename = HashMap<Vec<u8>, HashMap<Vec<u8>, Vec<u8>>>;

impl Spread {
    pub(super) fn append(
        &mut self,
        output: &mut Document,
        copier: &mut CopyObjects<'_>,
        page: ObjectId,
        place: Placement,
    ) -> Result<()> {
        let doc = copier.input;
        let attributes = doc.get_dictionary(page)?;
        ensure!(
            !attributes.has(b"Group"),
            "PDF page has a transparency group"
        );
        let mut rename = Rename::new();
        if let Some(resources) = inherited(doc, page, b"Resources")? {
            let resources = resources.as_dict()?;
            validate_resources(copier, resources, 0)?;
            for (category, entries) in resources {
                if category == b"ProcSet" {
                    // Obsolete since PDF 1.4; it does not name stream operands.
                    continue;
                }
                if !self.resources.has(category) {
                    self.resources.set(category.clone(), Dictionary::new());
                }
                let merged = self.resources.get_mut(category)?.as_dict_mut()?;
                let names = rename.entry(category.clone()).or_default();
                for (name, value) in doc.dereference(entries)?.1.as_dict()? {
                    // Rename only collisions, scoped by resource category. Store the
                    // full assignment so lookups also reject missing input resources.
                    let mut assigned = name.clone();
                    while merged.has(&assigned) {
                        assigned = format!("Wenyi{}", self.next_name).into_bytes();
                        self.next_name += 1;
                    }
                    merged.set(assigned.clone(), copier.copy(output, value, 0)?);
                    names.insert(name.clone(), assigned);
                }
            }
        }

        let mut data = Vec::new();
        let mut stream_ends = Vec::new();
        if let Ok(content) = attributes.get(b"Contents") {
            match doc.dereference(content)?.1 {
                Object::Null => {}
                Object::Array(streams) => {
                    for value in streams {
                        append_stream(&mut data, doc.dereference(value)?.1.as_stream()?, doc)?;
                        stream_ends.push(data.len() - 1);
                    }
                }
                Object::Stream(stream) => append_stream(&mut data, stream, doc)?,
                _ => bail!("unsupported PDF page content object"),
            }
        }
        let [a, b, c, d, e, f] = place.matrix;
        let [x0, y0, x1, y1] = place.crop;
        writeln!(
            self.operations,
            "q\n{a} {b} {c} {d} {e} {f} cm\n{x0} {y0} {} {} re W n",
            x1 - x0,
            y1 - y0
        )?;
        rewrite(&data, &stream_ends, &rename, &mut self.operations)?;
        // The current path, unlike the clip and graphics state, isn't saved by q/Q.
        self.operations.extend_from_slice(b"\nn Q\n");
        Ok(())
    }
}

fn validate_resources(
    copier: &mut CopyObjects<'_>,
    resources: &Dictionary,
    depth: usize,
) -> Result<()> {
    ensure!(depth < 32, "cyclic or excessively deep PDF form resources");
    let doc = copier.input;
    for (category, entries) in resources {
        let entries = doc.dereference(entries)?.1;
        match category.as_slice() {
            b"ProcSet" => continue,
            b"Font" | b"XObject" | b"ExtGState" | b"Properties" => {}
            // In particular, DefaultRGB/Gray/CMYK affect implicit colour selection
            // and can't be isolated by renaming the CS/cs operands.
            _ if matches!(entries, Object::Dictionary(dict) if dict.is_empty()) => continue,
            _ => bail!(
                "unsupported PDF resource category /{}",
                String::from_utf8_lossy(category)
            ),
        }
        for (_, resource) in entries.as_dict()? {
            let (id, resource) = doc.dereference(resource)?;
            match category.as_slice() {
                b"Font" => {
                    let font = resource.as_dict()?;
                    ensure!(
                        font.get_deref(b"Subtype", doc)?.as_name()? != b"Type3",
                        "PDF Type 3 font may depend on page resources"
                    );
                }
                b"XObject" => {
                    if id.is_some_and(|id| copier.checked_resources.contains(&id)) {
                        continue;
                    }
                    let stream = resource.as_stream()?;
                    ensure!(
                        ![b"OC".as_slice(), b"Ref", b"OPI"]
                            .iter()
                            .any(|key| stream.dict.has(key)),
                        "PDF XObject depends on optional or external content"
                    );
                    match stream.dict.get_deref(b"Subtype", doc)?.as_name()? {
                        b"Image" => {
                            if let Ok(color) = stream.dict.get(b"ColorSpace") {
                                ensure!(
                                    device_color(doc.dereference(color)?.1.as_name()?),
                                    "PDF image uses a non-device colour space"
                                );
                            }
                            for key in [b"SMask".as_slice(), b"Mask"] {
                                if let Ok(mask) = stream.dict.get(key) {
                                    ensure!(
                                        !matches!(doc.dereference(mask)?.1, Object::Stream(_)),
                                        "PDF image has a dependent mask image"
                                    );
                                }
                            }
                        }
                        b"Form" => {
                            // Legacy forms with no resource dictionary can use the
                            // page namespace. Keep only forms with a local namespace.
                            let nested = stream
                                .dict
                                .get_deref(b"Resources", doc)
                                .context("PDF Form XObject has no local resources")?
                                .as_dict()?;
                            validate_resources(copier, nested, depth + 1)?;
                        }
                        _ => bail!("unsupported PDF XObject type"),
                    }
                    if let Some(id) = id {
                        copier.checked_resources.insert(id);
                    }
                }
                b"ExtGState" => {
                    let state = resource.as_dict()?;
                    ensure!(
                        !state.has(b"Font")
                            && (!state.has(b"SMask")
                                || matches!(
                                    state.get_deref(b"SMask", doc)?.as_name(),
                                    Ok(b"None")
                                )),
                        "PDF graphics state uses a font or soft mask"
                    );
                }
                _ => {
                    let property = resource.as_dict()?;
                    ensure!(
                        !matches!(
                            property.get_deref(b"Type", doc).and_then(Object::as_name),
                            Ok(b"OCG" | b"OCMD")
                        ),
                        "optional PDF marked content"
                    );
                }
            }
        }
    }
    Ok(())
}

fn append_stream(data: &mut Vec<u8>, stream: &Stream, doc: &Document) -> Result<()> {
    let budget = lopdf::DEFAULT_MAX_DECOMPRESSED_SIZE.saturating_sub(data.len());
    if stream.dict.has(b"Filter") {
        // Resolve top-level indirect filters/parameters before invoking the decoder.
        let mut stream = stream.clone();
        for key in [b"Filter".as_slice(), b"DecodeParms"] {
            if let Ok(value) = stream.dict.get(key) {
                stream.dict.set(key, doc.dereference(value)?.1.clone());
            }
        }
        stream.filters()?;
        data.extend_from_slice(&stream.decompressed_content_with_limit(budget)?);
    } else {
        ensure!(
            stream.content.len() <= budget,
            "PDF page content exceeds the native limit"
        );
        data.extend_from_slice(&stream.content);
    }
    // PDF treats a content array as a sequence of streams sharing graphic/text state.
    // Don't let a trailing comment or regular token swallow the next stream.
    data.push(b'\n');
    Ok(())
}

/// Lex only what is needed to locate top-level operands. Preserve all other bytes:
/// reserializing parsed operations can round graphics coordinates or change glyph bytes.
struct Tokens<'a> {
    data: &'a [u8],
    pos: usize,
}

impl Tokens<'_> {
    fn peek(&self) -> Option<u8> {
        self.data.get(self.pos).copied()
    }

    fn space(&mut self) {
        while let Some(byte) = self.peek() {
            if is_space(byte) {
                self.pos += 1;
            } else if byte == b'%' {
                while self.peek().is_some_and(|b| !matches!(b, b'\r' | b'\n')) {
                    self.pos += 1;
                }
            } else {
                break;
            }
        }
    }

    fn token(&mut self, depth: usize) -> Result<Range<usize>> {
        ensure!(depth < 64, "excessively deep PDF content operand");
        let start = self.pos;
        let first = self.peek().context("unterminated PDF content operand")?;
        self.pos += 1;
        match first {
            b'(' => {
                let mut parens = 1;
                while parens != 0 {
                    let byte = self.peek().context("unterminated PDF literal string")?;
                    self.pos += 1;
                    match byte {
                        b'\\' if self.peek().is_some() => self.pos += 1,
                        b'(' => parens += 1,
                        b')' => parens -= 1,
                        _ => {}
                    }
                }
            }
            b'<' if self.peek() != Some(b'<') => {
                while let Some(byte) = self.peek() {
                    self.pos += 1;
                    if byte == b'>' {
                        return Ok(start..self.pos);
                    }
                    ensure!(
                        byte.is_ascii_hexdigit() || is_space(byte),
                        "invalid PDF hex string"
                    );
                }
                bail!("unterminated PDF hex string");
            }
            b'[' | b'<' => {
                let close = if first == b'[' {
                    &b"]"[..]
                } else {
                    self.pos += 1;
                    &b">>"[..]
                };
                loop {
                    self.space();
                    if self.data[self.pos..].starts_with(close) {
                        self.pos += close.len();
                        break;
                    }
                    self.token(depth + 1)?;
                }
            }
            b'/' => self.regular(),
            byte if is_delimiter(byte) => bail!("invalid PDF content delimiter"),
            _ => self.regular(),
        }
        Ok(start..self.pos)
    }

    fn regular(&mut self) {
        while self
            .peek()
            .is_some_and(|b| !is_space(b) && !is_delimiter(b))
        {
            self.pos += 1;
        }
    }
}

fn is_space(byte: u8) -> bool {
    matches!(byte, 0 | b'\t' | b'\n' | 0x0c | b'\r' | b' ')
}

fn is_delimiter(byte: u8) -> bool {
    b"()<>[]{}/%".contains(&byte)
}

fn pdf_name(token: &[u8]) -> Result<Vec<u8>> {
    let mut name = Vec::with_capacity(token.len());
    let mut bytes = token
        .strip_prefix(b"/")
        .context("expected a PDF name operand")?;
    while let Some((&byte, rest)) = bytes.split_first() {
        bytes = rest;
        if byte == b'#' {
            let hex = bytes.get(..2).context("incomplete PDF name escape")?;
            let high = (hex[0] as char)
                .to_digit(16)
                .context("invalid PDF name escape")?;
            let low = (hex[1] as char)
                .to_digit(16)
                .context("invalid PDF name escape")?;
            name.push((high * 16 + low) as u8);
            bytes = &bytes[2..];
        } else {
            name.push(byte);
        }
    }
    Ok(name)
}

fn device_color(name: &[u8]) -> bool {
    matches!(name, b"DeviceGray" | b"DeviceRGB" | b"DeviceCMYK")
}

fn check_operator(operator: &[u8], arity: usize) -> Result<()> {
    let expected = match operator {
        b"q" | b"Q" | b"h" | b"S" | b"s" | b"f" | b"F" | b"f*" | b"B" | b"B*" | b"b" | b"b*"
        | b"n" | b"W" | b"W*" | b"BT" | b"ET" | b"T*" | b"EMC" => 0,
        b"w" | b"J" | b"j" | b"M" | b"ri" | b"i" | b"Tc" | b"Tw" | b"Tz" | b"TL" | b"Tr"
        | b"Ts" | b"Tj" | b"TJ" | b"'" | b"G" | b"g" | b"CS" | b"cs" | b"Do" | b"gs" | b"MP"
        | b"BMC" => 1,
        b"d" | b"m" | b"l" | b"Td" | b"TD" | b"Tf" | b"DP" | b"BDC" => 2,
        b"RG" | b"rg" | b"\"" => 3,
        b"v" | b"y" | b"re" | b"K" | b"k" => 4,
        b"cm" | b"c" | b"Tm" => 6,
        b"SC" | b"sc" | b"SCN" | b"scn" if (1..=4).contains(&arity) => arity,
        b"BI" => bail!("inline PDF image needs the compatibility writer"),
        _ => bail!(
            "unsupported PDF content operator {}",
            String::from_utf8_lossy(operator)
        ),
    };
    ensure!(arity == expected, "invalid PDF operator operands");
    Ok(())
}

fn rewrite(
    data: &[u8],
    mut stream_ends: &[usize],
    names: &Rename,
    output: &mut Vec<u8>,
) -> Result<()> {
    let mut tokens = Tokens { data, pos: 0 };
    let mut operands = Vec::new();
    let mut copied = 0;
    let mut graphics = 0usize;
    let mut text = false;
    let mut marked = 0usize;
    loop {
        tokens.space();
        if tokens.peek().is_none() {
            break;
        }
        let range = tokens.token(0)?;
        while stream_ends.first().is_some_and(|end| *end < range.start) {
            stream_ends = &stream_ends[1..];
        }
        ensure!(
            !stream_ends.first().is_some_and(|end| *end < range.end),
            "PDF content operand spans multiple streams"
        );
        let token = &data[range.clone()];
        if matches!(
            token[0],
            b'/' | b'(' | b'<' | b'[' | b'+' | b'-' | b'.' | b'0'..=b'9'
        ) || matches!(token, b"null" | b"true" | b"false")
        {
            operands.push(range);
            ensure!(operands.len() <= 32, "too many PDF operator operands");
            continue;
        }

        check_operator(token, operands.len())?;
        // The arity table above guarantees the operands accessed below exist.
        let operand = |i: usize| &data[operands[i].clone()];
        let resource: Option<(&[u8], usize)> = match token {
            b"Tf" => Some((b"Font", 0)),
            b"Do" => Some((b"XObject", 0)),
            b"gs" => Some((b"ExtGState", 0)),
            b"BDC" | b"DP" | b"BMC" | b"MP" => {
                ensure!(
                    pdf_name(operand(0))? != b"OC",
                    "optional PDF marked content"
                );
                if matches!(token, b"BMC" | b"BDC") {
                    marked += 1;
                }
                match token {
                    b"DP" | b"BDC" if operand(1).starts_with(b"/") => Some((b"Properties", 1)),
                    b"DP" | b"BDC" => {
                        ensure!(
                            operand(1).starts_with(b"<<"),
                            "unsupported PDF marked properties"
                        );
                        None
                    }
                    _ => None,
                }
            }
            b"EMC" => {
                marked = marked
                    .checked_sub(1)
                    .context("unbalanced PDF marked content")?;
                None
            }
            b"q" => {
                graphics += 1;
                None
            }
            b"Q" => {
                graphics = graphics
                    .checked_sub(1)
                    .context("unbalanced PDF graphics state")?;
                None
            }
            b"BT" => {
                ensure!(!text, "nested PDF text object");
                text = true;
                None
            }
            b"ET" => {
                ensure!(text, "unbalanced PDF text object");
                text = false;
                None
            }
            b"CS" | b"cs" => {
                ensure!(
                    device_color(&pdf_name(operand(0))?),
                    "unsupported PDF colour space"
                );
                None
            }
            b"SC" | b"sc" | b"SCN" | b"scn" => {
                ensure!(
                    !operands.iter().any(|r| data[r.start] == b'/'),
                    "PDF pattern colour"
                );
                None
            }
            // check_operator already excluded anything outside the known subset.
            _ => None,
        };
        if let Some((category, index)) = resource {
            let name = pdf_name(operand(index))?;
            let assigned = names
                .get(category)
                .and_then(|entries| entries.get(&name))
                .with_context(|| {
                    format!("missing PDF resource /{}", String::from_utf8_lossy(&name))
                })?;
            if assigned != &name {
                let range = &operands[index];
                output.extend_from_slice(&data[copied..range.start]);
                output.push(b'/');
                output.extend_from_slice(assigned);
                copied = range.end;
            }
        }
        operands.clear();
    }
    ensure!(
        graphics == 0 && !text && marked == 0 && operands.is_empty(),
        "unbalanced PDF page content"
    );
    output.extend_from_slice(&data[copied..]);
    Ok(())
}
