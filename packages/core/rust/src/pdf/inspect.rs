//! BabelDOC input fast paths. An inconclusive result leaves interpretation to pypdf.

use std::{
    collections::{HashMap, HashSet},
    path::PathBuf,
};

use anyhow::{ensure, Context, Result};
use lopdf::{content::Content, Dictionary, Document, LoadOptions, Object, ObjectId, Stream};
use pyo3::{exceptions::PyValueError, prelude::*};

const MAX_DEPTH: usize = 64;
const MAX_CONTENT_BYTES: usize = 16 * 1024 * 1024;

pub(super) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PdfInspection>()
}

#[pyclass(frozen)]
struct PdfInspection {
    doc: Document,
    pages: Vec<ObjectId>,
}

#[pymethods]
impl PdfInspection {
    #[new]
    fn new(py: Python<'_>, path: PathBuf) -> PyResult<Self> {
        py.detach(|| Self::open(path))
            .map_err(|error| PyValueError::new_err(error.to_string()))
    }

    #[getter]
    fn page_count(&self) -> usize {
        self.pages.len()
    }

    fn image_only(&self, py: Python<'_>, indices: Vec<usize>) -> Option<bool> {
        py.detach(|| self.inspect_pages(&indices).ok().flatten())
    }

    fn outline(&self, py: Python<'_>) -> Option<Vec<(usize, String, usize)>> {
        py.detach(|| self.read_outline().ok())
    }
}

impl PdfInspection {
    fn open(path: PathBuf) -> Result<Self> {
        let doc = Document::load_with_options(
            path,
            LoadOptions {
                strict: true,
                ..Default::default()
            },
        )?;
        ensure!(!doc.is_encrypted() && doc.encryption_state.is_none());
        let catalog = doc.catalog()?;
        ensure!(catalog.get(b"Type")?.as_name()? == b"Catalog");
        let mut pages = Vec::new();
        collect_pages(
            &doc,
            catalog.get(b"Pages")?.as_reference()?,
            None,
            &mut pages,
            &mut HashSet::new(),
            0,
        )?;
        Ok(Self { doc, pages })
    }

    fn inspect_pages(&self, indices: &[usize]) -> Result<Option<bool>> {
        let mut found_image = false;
        let mut uncertain_text = false;
        let mut images = HashMap::new();
        let mut forms = HashMap::new();
        for &index in indices {
            let page_id = *self.pages.get(index).context("invalid selection")?;
            let page = self.doc.get_dictionary(page_id)?;
            let resources = page_resources(&self.doc, page_id)?;
            let contents = read_contents(&self.doc, page.get(b"Contents").ok())?;
            let text = inspect_content(&self.doc, &contents, resources, &mut forms, 0)?;
            if text == Text::Present {
                return Ok(Some(false));
            }
            uncertain_text |= text == Text::Unknown;
            found_image =
                found_image || resources_have_image(&self.doc, resources, &mut images, 0)?;
        }
        // Unknown font mappings don't matter if there isn't an image on any selected page.
        Ok(if found_image && uncertain_text {
            None
        } else {
            Some(found_image)
        })
    }

    fn read_outline(&self) -> Result<Vec<(usize, String, usize)>> {
        let catalog = self.doc.catalog()?;
        let mut output = Vec::new();
        // Named destinations and nonstandard outline entries retain pypdf's recovery rules.
        ensure!(!catalog.has(b"Dests"));
        if let Ok(names) = catalog.get_deref(b"Names", &self.doc) {
            ensure!(!names.as_dict()?.has(b"Dests"));
        } else {
            ensure!(!catalog.has(b"Names"));
        }
        let Ok(root) = catalog.get(b"Outlines") else {
            return Ok(output);
        };
        let root = self.doc.dereference(root)?.1;
        if matches!(root, Object::Null) {
            return Ok(output);
        }
        // Push the next sibling before the first child to preserve pypdf's depth-first order.
        let root = root.as_dict()?;
        let Ok(first) = root.get(b"First") else {
            return Ok(output);
        };
        let pages = self
            .pages
            .iter()
            .enumerate()
            .map(|(i, &id)| (id, i))
            .collect();
        let mut pending = vec![(first.as_reference()?, 0)];
        let mut seen = HashSet::new();
        while let Some((id, depth)) = pending.pop() {
            ensure!(depth < MAX_DEPTH && seen.insert(id));
            let node = self.doc.get_dictionary(id)?;
            let title = node.get_deref(b"Title", &self.doc)?.as_str()?;
            // pypdf also autodetects little-endian and BOM-less UTF-16. Use it for
            // ambiguous strings rather than silently changing a title or dropping it.
            let title = if title.iter().all(|byte| matches!(byte, b' '..=b'~')) {
                std::str::from_utf8(title)?.to_owned()
            } else {
                ensure!(title.starts_with(b"\xfe\xff") && title.len() % 2 == 0);
                lopdf::decode_text_string(node.get_deref(b"Title", &self.doc)?)?
            };
            if let Ok(color) = node.get_deref(b"C", &self.doc) {
                ensure!(color.as_array()?.iter().all(number));
            } else {
                ensure!(!node.has(b"C"));
            }
            if let Ok(count) = node.get(b"Count") {
                ensure!(number(count));
            }
            if node.has(b"F") {
                node.get_deref(b"F", &self.doc)?.as_i64()?;
            }
            if let Some(page) = bookmark_page(&self.doc, node, &pages)? {
                output.push((depth, title, page));
            }
            if let Ok(next) = node.get(b"Next") {
                pending.push((next.as_reference()?, depth));
            }
            if let Ok(first) = node.get(b"First") {
                pending.push((first.as_reference()?, depth + 1));
            }
        }
        Ok(output)
    }
}

fn collect_pages(
    doc: &Document,
    id: ObjectId,
    parent: Option<ObjectId>,
    pages: &mut Vec<ObjectId>,
    seen: &mut HashSet<ObjectId>,
    depth: usize,
) -> Result<()> {
    ensure!(depth < MAX_DEPTH && seen.insert(id));
    // pypdf flattens the /Kids tree, but consults /Parent for inherited resources.
    let node = doc
        .objects
        .get(&id)
        .context("missing page tree node")?
        .as_dict()?;
    match parent {
        Some(parent) => ensure!(node.get(b"Parent")?.as_reference()? == parent),
        None => ensure!(!node.has(b"Parent")),
    }
    match node.get(b"Type")?.as_name()? {
        b"Page" if !node.has(b"Kids") => pages.push(id),
        b"Pages" => {
            let start = pages.len();
            for kid in node.get_deref(b"Kids", doc)?.as_array()? {
                collect_pages(doc, kid.as_reference()?, Some(id), pages, seen, depth + 1)?;
            }
            ensure!(node.get(b"Count")?.as_i64()? == (pages.len() - start) as i64);
        }
        _ => anyhow::bail!("invalid page tree node"),
    }
    Ok(())
}

fn page_resources(doc: &Document, page: ObjectId) -> Result<Option<&Dictionary>> {
    let node = doc.get_dictionary(page)?;
    if node.has(b"Resources") {
        return optional_dict(doc, node, b"Resources");
    }
    // pypdf's flattening can retain inherited resources across nested siblings.
    // Don't approximate those semantics for leaves inheriting from an intermediate branch.
    let parent = doc.get_dictionary(node.get(b"Parent")?.as_reference()?)?;
    ensure!(!parent.has(b"Parent"));
    optional_dict(doc, parent, b"Resources")
}

fn optional_dict<'a>(
    doc: &'a Document,
    dict: &'a Dictionary,
    key: &[u8],
) -> Result<Option<&'a Dictionary>> {
    if dict.has(key) {
        Ok(Some(dict.get_deref(key, doc)?.as_dict()?))
    } else {
        Ok(None)
    }
}

fn xobject<'a>(doc: &'a Document, object: &'a Object) -> Result<(&'a Stream, &'a [u8])> {
    let stream = doc.dereference(object)?.1.as_stream()?;
    let subtype = stream.dict.get(b"Subtype")?.as_name()?;
    ensure!(matches!(subtype, b"Form" | b"Image"));
    Ok((stream, subtype))
}

// Cache shared resource dictionaries. A None entry means they are on the active path;
// leave cycles to pypdf, which also guards the walk by resource identity.
fn resources_have_image(
    doc: &Document,
    resources: Option<&Dictionary>,
    cache: &mut HashMap<*const Dictionary, Option<bool>>,
    depth: usize,
) -> Result<bool> {
    let Some(resources) = resources else {
        return Ok(false);
    };
    let id = std::ptr::from_ref(resources);
    ensure!(depth < MAX_DEPTH);
    if let Some(state) = cache.get(&id) {
        return state.context("cyclic form resources");
    }
    cache.insert(id, None);
    let mut image = false;
    if let Some(objects) = optional_dict(doc, resources, b"XObject")? {
        for (_, object) in objects.iter() {
            let (stream, subtype) = xobject(doc, object)?;
            if subtype == b"Image"
                || resources_have_image(
                    doc,
                    optional_dict(doc, &stream.dict, b"Resources")?,
                    cache,
                    depth + 1,
                )?
            {
                image = true;
                break;
            }
        }
    }
    cache.insert(id, Some(image));
    Ok(image)
}

fn read_contents(doc: &Document, contents: Option<&Object>) -> Result<Vec<u8>> {
    let mut bytes = Vec::new();
    if let Some(object) = contents {
        match doc.dereference(object)?.1 {
            Object::Array(streams) => {
                ensure!(streams.len() <= 1000);
                for stream in streams {
                    append_stream(doc.dereference(stream)?.1.as_stream()?, &mut bytes)?;
                    if !bytes.ends_with(b"\n") {
                        ensure!(bytes.len() < MAX_CONTENT_BYTES);
                        bytes.push(b'\n');
                    }
                }
            }
            Object::Stream(stream) => append_stream(stream, &mut bytes)?,
            _ => anyhow::bail!("unexpected page contents"),
        }
    }
    Ok(bytes)
}

fn append_stream(stream: &Stream, bytes: &mut Vec<u8>) -> Result<()> {
    let remaining = MAX_CONTENT_BYTES - bytes.len();
    if stream.dict.has(b"Filter") {
        // Do not use lopdf's page-content helper, which may silently use raw data
        // on decode errors. Other filters and predictors go through pypdf.
        ensure!(
            stream.filters()?.as_slice() == [b"FlateDecode"] && !stream.dict.has(b"DecodeParms")
        );
        bytes.extend(stream.decompressed_content_with_limit(remaining)?);
    } else {
        ensure!(stream.content.len() <= remaining);
        bytes.extend_from_slice(&stream.content);
    }
    Ok(())
}

#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
enum Text {
    Absent,
    Unknown,
    Present,
}

fn inspect_content(
    doc: &Document,
    bytes: &[u8],
    resources: Option<&Dictionary>,
    forms: &mut HashMap<*const Stream, Option<Text>>,
    depth: usize,
) -> Result<Text> {
    ensure!(depth < MAX_DEPTH);
    let fonts = resources
        .map(|r| optional_dict(doc, r, b"Font"))
        .transpose()?
        .flatten();
    let objects = resources
        .map(|r| optional_dict(doc, r, b"XObject"))
        .transpose()?
        .flatten();
    let mut standard_fonts = HashSet::new();
    let mut text = Text::Absent;
    if let Some(fonts) = fonts {
        // pypdf reads all fonts up front, so an unfamiliar resource prevents us
        // from certifying an image-only page even if it has no show-text operators.
        for (name, object) in fonts.iter() {
            if standard_font(doc.dereference(object)?.1.as_dict()?) {
                standard_fonts.insert(name.as_slice());
            } else {
                text = Text::Unknown;
            }
        }
    }
    let mut ascii_font = false;
    for op in Content::decode_strict(bytes)?.operations {
        let args = op.operands.as_slice();
        match op.operator.as_str() {
            "Tf" => {
                ensure!(args.len() == 2 && number(&args[1]));
                ascii_font = standard_fonts.contains(args[0].as_name()?);
            }
            "Q" => ascii_font = false,
            "Tj" | "TJ" | "'" | "\"" => {
                let strings = match (op.operator.as_str(), args) {
                    ("TJ", [Object::Array(items)]) => items.as_slice(),
                    ("Tj" | "'", [Object::String(_, _)]) => args,
                    ("\"", [word, char, value @ Object::String(_, _)])
                        if number(word) && number(char) =>
                    {
                        std::slice::from_ref(value)
                    }
                    _ => anyhow::bail!("unexpected show-text operands"),
                };
                for string in strings {
                    if let Object::String(raw, _) = string {
                        if !raw.is_empty() {
                            if ascii_font && raw.iter().all(|b| matches!(b, b' '..=b'~')) {
                                if raw.iter().any(|b| *b != b' ') {
                                    return Ok(Text::Present);
                                }
                            } else {
                                text = Text::Unknown;
                            }
                        }
                    } else {
                        ensure!(number(string));
                    }
                }
            }
            "Do" => {
                ensure!(args.len() == 1);
                let obj = objects
                    .context("missing XObject resources")?
                    .get(args[0].as_name()?)?;
                let (stream, subtype) = xobject(doc, obj)?;
                if subtype == b"Form" {
                    let id = std::ptr::from_ref(stream);
                    let form = if let Some(state) = forms.get(&id) {
                        state.context("cyclic form content")?
                    } else {
                        // An absent resource dictionary is unambiguous only if
                        // pypdf won't follow a nonstandard /Parent pointer.
                        ensure!(!stream.dict.has(b"Parent"));
                        forms.insert(id, None);
                        let mut contents = Vec::new();
                        append_stream(stream, &mut contents)?;
                        let form = inspect_content(
                            doc,
                            &contents,
                            optional_dict(doc, &stream.dict, b"Resources")?,
                            forms,
                            depth + 1,
                        )?;
                        forms.insert(id, Some(form));
                        form
                    };
                    if form == Text::Present {
                        return Ok(form);
                    }
                    text = text.max(form);
                }
            }
            // The parsers recover different EI boundaries for some inline images.
            "BI" | "ID" | "EI" => anyhow::bail!("inline image requires pypdf"),
            // The only remaining operations whose operands pypdf's text engine reads.
            "cm" | "Tm" => ensure!(args.len() == 6 && args.iter().all(number)),
            "Td" | "TD" => ensure!(args.len() == 2 && args.iter().all(number)),
            "TL" | "Tz" | "Tw" => ensure!(args.len() == 1 && number(&args[0])),
            _ => {}
        }
    }
    Ok(text)
}

fn number(object: &Object) -> bool {
    match object {
        Object::Integer(_) => true,
        Object::Real(value) => value.is_finite(),
        _ => false,
    }
}

fn standard_font(font: &Dictionary) -> bool {
    matches!(font.get(b"Subtype").and_then(Object::as_name), Ok(b"Type1"))
        && matches!(
            font.get(b"BaseFont").and_then(Object::as_name),
            Ok(b"Helvetica"
                | b"Helvetica-Bold"
                | b"Helvetica-Oblique"
                | b"Helvetica-BoldOblique"
                | b"Times-Roman"
                | b"Times-Bold"
                | b"Times-Italic"
                | b"Times-BoldItalic"
                | b"Courier"
                | b"Courier-Bold"
                | b"Courier-Oblique"
                | b"Courier-BoldOblique")
        )
        && (!font.has(b"Encoding")
            || matches!(
                font.get(b"Encoding").and_then(Object::as_name),
                Ok(b"WinAnsiEncoding" | b"MacRomanEncoding" | b"StandardEncoding")
            ))
        && !font.has(b"ToUnicode")
        && !font.has(b"Widths")
        && !font.has(b"FontDescriptor")
        && !font.has(b"FontBBox")
}

fn bookmark_page(
    doc: &Document,
    node: &Dictionary,
    pages: &HashMap<ObjectId, usize>,
) -> Result<Option<usize>> {
    let dest = if node.has(b"A") {
        let action = node.get_deref(b"A", doc)?.as_dict()?;
        ensure!(action.get(b"S")?.as_name()? == b"GoTo" && !node.has(b"Dest"));
        action.get_deref(b"D", doc)?
    } else if node.has(b"Dest") {
        node.get_deref(b"Dest", doc)?
    } else {
        // A heading without a destination still introduces a nested level.
        return Ok(None);
    };
    let [page, fit, params @ ..] = dest.as_array()?.as_slice() else {
        anyhow::bail!("invalid explicit destination");
    };
    let expected = match fit.as_name()? {
        b"Fit" | b"FitB" => 0,
        b"FitH" | b"FitV" | b"FitBH" | b"FitBV" => 1,
        b"XYZ" => 3,
        b"FitR" => 4,
        _ => anyhow::bail!("unsupported destination fit"),
    };
    ensure!(
        params.len() == expected
            && params
                .iter()
                .all(|p| number(p) || matches!(p, Object::Null))
    );
    Ok(Some(
        *pages
            .get(&page.as_reference()?)
            .context("destination is not a page")?,
    ))
}
