//! Native WordprocessingML import and reconstruction. Python owns the store and export policy.

mod export;
mod ingest;
mod opc;
mod project;
mod properties;

use std::{path::PathBuf, sync::LazyLock};

use anyhow::Context;
use pyo3::{
    prelude::*,
    types::{PyBytes, PyIterator},
};
use regex::Regex;

fn has_list_prefix(text: &str) -> bool {
    static PREFIX: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"(?i)^(?:\d+\.|[a-z]\.|[ivxlcdm]+\.|[•·‣▪◦]\s*|（?\d+）|\(\d+\))\s+")
            .expect("valid list prefix")
    });
    PREFIX.is_match(text.trim_start())
}

#[pyfunction]
fn parse_docx<'py>(
    py: Python<'py>,
    path: PathBuf,
    title: &str,
    source_path: &str,
    source_lang: &str,
    target_lang: &str,
) -> PyResult<Bound<'py, PyBytes>> {
    let data = py
        .detach(|| ingest::parse(&path, title, source_path, source_lang, target_lang))
        .map_err(crate::value_error)?;
    Ok(PyBytes::new(py, &data))
}

#[pyfunction]
fn write_docx(
    py: Python<'_>,
    destination: PathBuf,
    header: &[u8],
    chapters: Bound<'_, PyIterator>,
) -> PyResult<()> {
    let (mut writer, options) = py
        .detach(|| {
            let header: project::Header =
                serde_json::from_slice(header).context("Invalid DOCX export header")?;
            header.start()
        })
        .map_err(crate::value_error)?;
    // Load the next snapshot chapter under Python; decode and render it without
    // holding the GIL. Never open the destination if reading any chapter fails.
    for (i, chapter) in chapters.enumerate() {
        let bytes = chapter?.cast_into::<PyBytes>()?;
        let bytes = bytes.as_bytes();
        py.detach(|| {
            let chapter: project::Chapter = serde_json::from_slice(bytes)
                .with_context(|| format!("Invalid DOCX export chapter {}", i + 1))?;
            chapter.write(&mut writer, &options)
        })
        .map_err(crate::value_error)?;
    }
    py.detach(|| writer.finish(&destination))
        .map_err(crate::value_error)
}

pub(crate) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(parse_docx, m)?)?;
    m.add_function(wrap_pyfunction!(write_docx, m)?)?;
    Ok(())
}
