//! Python bindings for Core's native document engines.

mod docx;
mod epub;
mod fb2;
mod html;
mod pdf;
mod text;

use std::{collections::HashMap, path::PathBuf};

use epub::{archive, export as writer, generate, ingest as reader, workers, Token};
use pyo3::{
    exceptions::{PyOSError, PyValueError},
    prelude::*,
    types::PyBytes,
};

fn value_error(error: anyhow::Error) -> PyErr {
    let message = format!("{error:#}");
    if error.chain().any(|cause| cause.is::<std::io::Error>()) {
        PyOSError::new_err(message)
    } else {
        PyValueError::new_err(message)
    }
}

#[pyfunction]
fn xhtml_tokens<'a>(py: Python<'_>, markup: &'a str) -> Option<Vec<Token<'a>>> {
    py.detach(|| epub::xhtml_tokens(markup))
}

#[pyfunction]
fn peek(py: Python<'_>, path: PathBuf) -> PyResult<String> {
    py.detach(|| {
        let mut zip = archive::open(&path)?;
        Ok(reader::book_title(&path, &reader::package(&mut zip)?))
    })
    .map_err(value_error)
}

#[pyfunction]
fn ingest<'py>(
    py: Python<'py>,
    path: PathBuf,
    source_lang: &str,
    target_lang: &str,
) -> PyResult<Bound<'py, PyBytes>> {
    let data = py
        .detach(|| {
            let _admission = workers::admit();
            reader::ingest(&path, source_lang, target_lang)
        })
        .map_err(value_error)?;
    Ok(PyBytes::new(py, &data))
}

#[pyfunction]
fn export(
    py: Python<'_>,
    source: PathBuf,
    destination: PathBuf,
    options: &[u8],
) -> PyResult<Vec<String>> {
    py.detach(|| {
        let _admission = workers::admit();
        writer::export(&source, &destination, serde_json::from_slice(options)?)
    })
    .map_err(value_error)
}

#[pyfunction]
fn render_resources(
    py: Python<'_>,
    source: PathBuf,
    options: &[u8],
) -> PyResult<(Vec<(String, String)>, Vec<String>)> {
    py.detach(|| {
        let _admission = workers::admit();
        let (pages, warnings) = writer::resources(
            &mut archive::open(&source)?,
            &mut serde_json::from_slice(options)?,
        )?;
        Ok((pages.into_iter().collect(), warnings))
    })
    .map_err(value_error)
}

#[pyfunction(name = "generate")]
fn generate_epub(
    py: Python<'_>,
    destination: PathBuf,
    options: &[u8],
    assets: Vec<generate::Asset>,
) -> PyResult<()> {
    py.detach(|| {
        let _admission = workers::admit();
        generate::generate(&destination, serde_json::from_slice(options)?, assets)
    })
    .map_err(value_error)
}

#[pyfunction]
fn render_html_bodies(
    py: Python<'_>,
    source: PathBuf,
    options: &[u8],
    mime_types: HashMap<String, String>,
) -> PyResult<(Vec<String>, Vec<String>)> {
    py.detach(|| {
        let _admission = workers::admit();
        epub::html::bodies(&source, serde_json::from_slice(options)?, &mime_types)
    })
    .map_err(value_error)
}

#[pyfunction]
fn append_about(
    py: Python<'_>,
    source: PathBuf,
    destination: PathBuf,
    page: &str,
) -> PyResult<bool> {
    py.detach(|| {
        let _admission = workers::admit();
        writer::append_about(&source, &destination, page)
    })
    .map_err(value_error)
}

#[pyfunction]
fn parse_text(py: Python<'_>, content: &str) -> Vec<text::Parsed> {
    py.detach(|| text::parse(content))
}

#[pyfunction]
fn write_text(
    py: Python<'_>,
    destination: PathBuf,
    chapters: text::Rendered,
    bilingual: bool,
    source_first: bool,
    markdown: bool,
) -> PyResult<()> {
    py.detach(|| text::write(&destination, chapters, bilingual, source_first, markdown))
        .map_err(|error| value_error(error.into()))
}

#[pymodule]
fn _epub_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(xhtml_tokens, m)?)?;
    m.add_function(wrap_pyfunction!(peek, m)?)?;
    m.add_function(wrap_pyfunction!(ingest, m)?)?;
    m.add_function(wrap_pyfunction!(export, m)?)?;
    m.add_function(wrap_pyfunction!(render_resources, m)?)?;
    m.add_function(wrap_pyfunction!(render_html_bodies, m)?)?;
    m.add_function(wrap_pyfunction!(append_about, m)?)?;
    m.add_function(wrap_pyfunction!(generate_epub, m)?)?;
    m.add_function(wrap_pyfunction!(parse_text, m)?)?;
    m.add_function(wrap_pyfunction!(write_text, m)?)?;
    m.add_function(wrap_pyfunction!(parse_fb2, m)?)?;
    m.add_function(wrap_pyfunction!(fb2_title, m)?)?;
    m.add_function(wrap_pyfunction!(fb2_binaries, m)?)?;
    docx::register(m)?;
    html::register(m)?;
    pdf::register(m)?;
    Ok(())
}

#[pyfunction]
fn parse_fb2<'py>(
    py: Python<'py>,
    content: &str,
    fallback: &str,
    path: &str,
    source_lang: &str,
    target_lang: &str,
) -> PyResult<Bound<'py, PyBytes>> {
    let data = py
        .detach(|| fb2::ingest(content, fallback, path, source_lang, target_lang))
        .map_err(value_error)?;
    Ok(PyBytes::new(py, &data))
}

#[pyfunction]
fn fb2_title(py: Python<'_>, content: &str, fallback: &str) -> PyResult<String> {
    py.detach(|| Ok(fb2::title(fb2::parse(content)?.root_element(), fallback)))
        .map_err(value_error)
}

#[pyfunction]
fn fb2_binaries(py: Python<'_>, content: &str) -> PyResult<Vec<(String, String, String)>> {
    py.detach(|| fb2::binaries(content)).map_err(value_error)
}
