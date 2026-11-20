//! Make private, page-only MinerU upload chunks without decoding content or image streams.

use super::{inherited, CopyObjects, Input};
use anyhow::{ensure, Result};
use lopdf::{dictionary, Document, Object, ObjectId};
use pyo3::{exceptions::PyValueError, prelude::*};
use std::{fs, path::PathBuf};

fn chunk(input: &Input, pages: &[ObjectId]) -> Result<Document> {
    let mut out = Document::with_version(if input.pdf2 { "2.0" } else { "1.7" });
    let tree = out.new_object_id();
    let mut kids = Vec::with_capacity(pages.len());
    let mut copier = CopyObjects::new(&input.doc);
    for &id in pages {
        let page = input.doc.get_dictionary(id)?;
        // Widgets, links to other pages, structural parents and other document-wide
        // dependencies need pypdf's established page-cloning policy.
        let mut copy = dictionary! { "Type" => "Page", "Parent" => tree };
        for (key, value) in page {
            match key.as_slice() {
                b"Type" | b"Parent" => {}
                b"Contents" | b"Resources" | b"MediaBox" | b"Rotate" | b"CropBox" | b"TrimBox"
                | b"ArtBox" | b"BleedBox" | b"UserUnit" | b"Group" => {
                    copy.set(key.clone(), copier.copy(&mut out, value, 0)?);
                }
                _ => anyhow::bail!("non-local or unrecognized page property"),
            }
        }
        for key in [b"MediaBox".as_slice(), b"CropBox", b"Resources", b"Rotate"] {
            if !copy.has(key) {
                if let Some(value) = inherited(&input.doc, id, key)? {
                    copy.set(key, copier.copy(&mut out, value, 0)?);
                }
            }
        }
        ensure!(copy.has(b"MediaBox"), "missing page bounds");
        kids.push(Object::Reference(out.add_object(copy)));
    }
    out.set_object(
        tree,
        dictionary! { "Type" => "Pages", "Count" => kids.len() as i64, "Kids" => kids },
    );
    let root = out.add_object(dictionary! { "Type" => "Catalog", "Pages" => tree });
    out.trailer.set("Root", root);
    Ok(out)
}

/// Return the page count, or None to retry with pypdf. `directory` is a private
/// TemporaryDirectory: a declined late page or an I/O failure never exposes an upload.
#[pyfunction]
pub(super) fn mineru_prepare_chunks(
    py: Python<'_>,
    source: PathBuf,
    directory: PathBuf,
    max_pages: usize,
) -> PyResult<Option<usize>> {
    if max_pages == 0 {
        return Err(PyValueError::new_err("max_pages must be positive"));
    }
    py.detach(|| {
        let Ok(input) = Input::new(&fs::read(source)?) else {
            return Ok(None);
        };
        let count = input.pages.len();
        if count > max_pages {
            for (i, pages) in input.pages.chunks(max_pages).enumerate() {
                let Ok(mut doc) = chunk(&input, pages) else {
                    return Ok(None);
                };
                let start = i * max_pages;
                doc.save(directory.join(format!(
                    "chunk_p{}-{}.pdf",
                    start + 1,
                    start + pages.len()
                )))?;
            }
        }
        Ok(Some(count))
    })
}
