//! EPUB engine; all ZIP and markup processing is independent of Python.

pub(crate) mod archive;
pub(crate) mod dom;
pub(crate) mod export;
pub(crate) mod extract;
pub(crate) mod generate;
pub(crate) mod html;
pub(crate) mod ingest;
mod links;
mod tokenizer;
pub(crate) mod workers;

pub(crate) use tokenizer::Token;

pub(crate) fn xhtml_tokens(markup: &str) -> Option<Vec<Token<'_>>> {
    tokenizer::scan(markup)
}
