//! Inspect, split and compose local PDFs. HTTP, state validation and publication stay in Python.

mod chunks;
mod flat;
mod geometry;
mod inspect;
mod links;

use std::{
    collections::{HashMap, HashSet},
    fs,
    path::{Path, PathBuf},
};

use anyhow::{bail, ensure, Context, Result};
use lopdf::{dictionary, Dictionary, Document, LoadOptions, Object, ObjectId, Stream};
use pyo3::{exceptions::PyOSError, prelude::*};

use self::geometry::Layout;
use self::links::NamedDestinations;

type Destinations = HashMap<ObjectId, (ObjectId, geometry::Placement)>;

pub(crate) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(chunks::mineru_prepare_chunks, m)?)?;
    inspect::register(m)?;
    m.add_class::<PdfSpreadSource>()?;
    m.add_function(wrap_pyfunction!(pdf_spread_source, m)?)?;
    m.add_function(wrap_pyfunction!(pdf_write_spreads, m)?)
}

#[pyclass(frozen, module = "wenyi_core.markup._epub_native")]
struct PdfSpreadSource(Input);

#[pymethods]
impl PdfSpreadSource {
    #[getter]
    fn page_count(&self) -> usize {
        self.0.pages.len()
    }
}

/// Keep the validated page tree for composition after Python selects pages and calls fillback.
/// A string means pypdf needs to read the source instead.
#[pyfunction]
fn pdf_spread_source<'py>(py: Python<'py>, source: &[u8]) -> PyResult<Bound<'py, PyAny>> {
    match py.detach(|| Input::new(source)) {
        Ok(input) => Ok(Bound::new(py, PdfSpreadSource(input))?.into_any()),
        Err(reason) => Ok(format!("{reason:#}").into_pyobject(py)?.into_any()),
    }
}

/// `None` means written; a reason means the caller should use the compatibility writer.
/// Composition must complete before the private fillback file is opened for writing.
#[pyfunction]
fn pdf_write_spreads(
    py: Python<'_>,
    source: &PdfSpreadSource,
    fillback: PathBuf,
    indices: Vec<usize>,
    source_first: bool,
) -> PyResult<Option<String>> {
    py.detach(|| write_spreads(&source.0, &fillback, &indices, source_first))
        .map_err(|error| PyOSError::new_err(error.to_string()))
}

fn write_spreads(
    source: &Input,
    fillback: &Path,
    indices: &[usize],
    source_first: bool,
) -> std::io::Result<Option<String>> {
    let target = fs::read(fillback)?;
    match compose(source, &target, indices, source_first) {
        Ok(mut output) => {
            output.save(fillback)?;
            Ok(None)
        }
        Err(reason) => Ok(Some(format!("{reason:#}"))),
    }
}

fn compose(
    source: &Input,
    target: &[u8],
    indices: &[usize],
    source_first: bool,
) -> Result<Document> {
    let target = Input::new(target).context("reading fillback PDF")?;
    ensure!(
        !indices.is_empty() && target.pages.len() == indices.len(),
        "fillback does not match the saved page selection"
    );
    let mut output = Document::with_version(if source.pdf2 || target.pdf2 {
        "2.0"
    } else {
        "1.7"
    });
    let tree = output.new_object_id();
    let mut spreads = Vec::with_capacity(indices.len());
    let mut source_dests = HashMap::with_capacity(indices.len());
    let mut target_dests = HashMap::with_capacity(indices.len());
    for (&index, &fillback) in indices.iter().zip(&target.pages) {
        let original = *source
            .pages
            .get(index)
            .context("invalid source page index")?;
        let source_layout = Layout::from_page(&source.doc, original)?;
        let target_layout = Layout::from_page(&target.doc, fillback)?;
        let width = source_layout.width + target_layout.width;
        let height = source_layout.height.max(target_layout.height);
        let (source_x, target_x) = if source_first {
            (0., source_layout.width)
        } else {
            (target_layout.width, 0.)
        };
        let spread = output.new_object_id();
        ensure!(
            source_dests
                .insert(original, (spread, source_layout.place(source_x, height)))
                .is_none(),
            "duplicate source page selection"
        );
        target_dests.insert(fillback, (spread, target_layout.place(target_x, height)));
        spreads.push((spread, original, fillback, width, height));
    }

    let mut source_objects = CopyObjects::new(&source.doc);
    let mut target_objects = CopyObjects::new(&target.doc);
    let mut kids = Vec::with_capacity(spreads.len());
    for (spread, source_page, target_page, width, height) in spreads {
        let source_part = (source, &mut source_objects, source_page, &source_dests);
        let target_part = (&target, &mut target_objects, target_page, &target_dests);
        let parts = if source_first {
            [source_part, target_part]
        } else {
            [target_part, source_part]
        };
        let mut content = flat::Spread::default();
        let mut annots = Vec::new();
        for (input, copier, page, destinations) in parts {
            content.append(&mut output, copier, page, destinations[&page].1)?;
            links::append(&mut output, copier, input, page, destinations, &mut annots)?;
        }
        let mut operations = Stream::new(Dictionary::new(), content.operations);
        operations.compress()?;
        let operations = output.add_object(operations);
        let mut page = dictionary! {
            "Type" => "Page",
            "Parent" => tree,
            "MediaBox" => geometry::rectangle([0., 0., width, height])?,
            "Resources" => content.resources,
            "Contents" => operations,
        };
        if !annots.is_empty() {
            page.set("Annots", annots);
        }
        output.set_object(spread, page);
        kids.push(Object::Reference(spread));
    }
    output.set_object(
        tree,
        dictionary! {
            "Type" => "Pages",
            "Count" => kids.len() as i64,
            "Kids" => kids,
        },
    );
    let catalog = output.add_object(dictionary! { "Type" => "Catalog", "Pages" => tree });
    output.trailer.set("Root", catalog);
    Ok(output)
}

struct Input {
    doc: Document,
    pages: Vec<ObjectId>,
    pdf2: bool,
    names: NamedDestinations,
}

impl Input {
    fn new(bytes: &[u8]) -> Result<Self> {
        let doc = Document::load_mem_with_options(
            bytes,
            LoadOptions {
                strict: true,
                ..Default::default()
            },
        )?;
        ensure!(
            !doc.is_encrypted() && doc.encryption_state.is_none(),
            "encrypted PDF"
        );
        let catalog = doc.catalog()?;
        ensure!(
            !catalog.has(b"OCProperties")
                && !catalog.has(b"Extensions")
                && !catalog.has(b"OutputIntents"),
            "PDF has catalog-level rendering dependencies"
        );
        // Check the actual tree, not just /Count. Inconsistent trees can number
        // pages differently in different readers; leave those to pypdf.
        let root = catalog.get(b"Pages")?.as_reference()?;
        let mut pages = Vec::new();
        read_pages(&doc, root, None, 0, &mut pages, &mut HashSet::new())?;
        let pdf2 = doc.version == "2.0"
            || matches!(
                catalog
                    .get_deref(b"Version", &doc)
                    .and_then(Object::as_name),
                Ok(b"2.0")
            );
        let names = NamedDestinations::load(&doc)?;
        Ok(Self {
            doc,
            pages,
            pdf2,
            names,
        })
    }
}

fn read_pages(
    doc: &Document,
    id: ObjectId,
    parent: Option<ObjectId>,
    depth: usize,
    pages: &mut Vec<ObjectId>,
    seen: &mut HashSet<ObjectId>,
) -> Result<()> {
    ensure!(depth < 128 && seen.insert(id), "inconsistent PDF page tree");
    let node = doc.get_dictionary(id)?;
    if let Some(parent) = parent {
        ensure!(
            node.get(b"Parent")?.as_reference()? == parent,
            "inconsistent PDF page ancestry"
        );
    } else {
        ensure!(!node.has(b"Parent"), "PDF page tree root has a parent");
    }
    match node.get_deref(b"Type", doc)?.as_name()? {
        b"Page" if parent.is_some() => pages.push(id),
        b"Pages" => {
            let start = pages.len();
            for child in node.get_deref(b"Kids", doc)?.as_array()? {
                read_pages(doc, child.as_reference()?, Some(id), depth + 1, pages, seen)?;
            }
            ensure!(
                node.get_deref(b"Count", doc)?.as_i64()? == (pages.len() - start) as i64,
                "inconsistent PDF page count"
            );
        }
        _ => bail!("unsupported object in PDF page tree"),
    }
    Ok(())
}

/// Follow the PDF page tree for inheritable attributes only. `/UserUnit`,
/// `/Contents`, `/Group` and `/Annots` are not inheritable.
fn inherited<'a>(doc: &'a Document, mut page: ObjectId, key: &[u8]) -> Result<Option<&'a Object>> {
    for _ in 0..128 {
        let node = doc.get_dictionary(page)?;
        if let Ok(value) = node.get(key) {
            let value = doc.dereference(value)?.1;
            if !matches!(value, Object::Null) {
                return Ok(Some(value));
            }
        }
        match node.get(b"Parent") {
            Ok(parent) => page = parent.as_reference()?,
            Err(_) => return Ok(None),
        }
    }
    bail!("cyclic or excessively deep PDF page ancestry")
}

/// Import only the dependency graph of a selected page's resources and retained
/// annotations. Per-input ID maps preserve sharing without leaking old page trees.
struct CopyObjects<'a> {
    input: &'a Document,
    ids: HashMap<ObjectId, ObjectId>,
    checked_resources: HashSet<ObjectId>,
}

impl<'a> CopyObjects<'a> {
    fn new(input: &'a Document) -> Self {
        Self {
            input,
            ids: HashMap::new(),
            checked_resources: HashSet::new(),
        }
    }

    fn copy(&mut self, out: &mut Document, object: &Object, depth: usize) -> Result<Object> {
        ensure!(depth < 128, "excessively deep PDF resource graph");
        Ok(match object {
            Object::Reference(id) => {
                if let Some(copied) = self.ids.get(id) {
                    return Ok(Object::Reference(*copied));
                }
                let original = self.input.get_object(*id)?;
                let copied = out.new_object_id();
                self.ids.insert(*id, copied);
                let value = if let Object::Stream(stream) = original {
                    Object::Stream(self.copy_stream(out, stream, depth + 1)?)
                } else {
                    self.copy(out, original, depth + 1)?
                };
                out.set_object(copied, value);
                Object::Reference(copied)
            }
            Object::Array(values) => Object::Array(
                values
                    .iter()
                    .map(|value| self.copy(out, value, depth + 1))
                    .collect::<Result<_>>()?,
            ),
            Object::Dictionary(dict) => Object::Dictionary(self.copy_dict(out, dict, depth)?),
            Object::Stream(stream) => {
                let stream = self.copy_stream(out, stream, depth + 1)?;
                Object::Reference(out.add_object(stream))
            }
            Object::Real(value) => {
                ensure!(value.is_finite(), "nonfinite PDF resource number");
                object.clone()
            }
            scalar => scalar.clone(),
        })
    }

    fn copy_dict(
        &mut self,
        out: &mut Document,
        dict: &Dictionary,
        depth: usize,
    ) -> Result<Dictionary> {
        ensure!(
            !matches!(
                dict.get_deref(b"Type", self.input)
                    .and_then(Object::as_name),
                Ok(b"Page" | b"Pages" | b"Catalog")
            ),
            "indirect dependency on an input page or catalog"
        );
        let mut copied = Dictionary::new();
        for (key, value) in dict {
            copied.set(key.clone(), self.copy(out, value, depth + 1)?);
        }
        Ok(copied)
    }

    fn copy_stream(&mut self, out: &mut Document, stream: &Stream, depth: usize) -> Result<Stream> {
        let mut dict = stream.dict.clone();
        dict.remove(b"Length");
        Ok(Stream::new(
            self.copy_dict(out, &dict, depth)?,
            stream.content.clone(),
        ))
    }
}
