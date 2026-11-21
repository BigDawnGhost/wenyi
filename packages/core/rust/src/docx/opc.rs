//! Read only the relevant OPC parts; never resolve a relationship outside the ZIP.

use std::{
    borrow::Cow,
    fs::File,
    io::{BufReader, Read},
    path::Path,
};

use anyhow::{bail, ensure, Context, Result};
use encoding_rs::{UTF_16BE, UTF_16LE};
use percent_encoding::percent_decode_str;
use roxmltree::Document;
use zip::ZipArchive;

const PART_LIMIT: u64 = 128 * 1024 * 1024;
const REL_NS: &str = "http://schemas.openxmlformats.org/package/2006/relationships";
pub(super) const TRANSITIONAL: &str =
    "http://schemas.openxmlformats.org/wordprocessingml/2006/main";
pub(super) const STRICT: &str = "http://purl.oclc.org/ooxml/wordprocessingml/main";

pub(super) struct Package(ZipArchive<BufReader<File>>);

impl Package {
    pub(super) fn open(path: &Path) -> Result<Self> {
        Ok(Self(
            ZipArchive::new(BufReader::new(File::open(path)?))
                .context("Invalid Word ZIP archive")?,
        ))
    }

    pub(super) fn part_name<'a>(&self, uri: &'a str) -> Cow<'a, str> {
        // The resolved URI is the ZIP name. Only try a legacy name if the
        // exact member is absent, and only unescape spaces and UTF-8 text.
        // Unescaping %25, for instance, could select a different valid URI.
        if !uri.contains('%')
            || self.0.index_for_name(uri).is_some()
            || !escaped_bytes(uri).all(|byte| matches!(byte, Some(b' ' | 0x80..=0xff)))
        {
            return Cow::Borrowed(uri);
        }
        percent_decode_str(uri)
            .decode_utf8()
            .ok()
            .filter(|name| self.0.index_for_name(name).is_some())
            .unwrap_or(Cow::Borrowed(uri))
    }

    pub(super) fn xml(&mut self, name: &str) -> Result<String> {
        let mut bytes = Vec::new();
        let file = self
            .0
            .by_name(name)
            .with_context(|| format!("Missing DOCX part {name}"))?;
        ensure!(file.size() <= PART_LIMIT, "DOCX part {name} is too large");
        file.take(PART_LIMIT + 1).read_to_end(&mut bytes)?;
        ensure!(
            bytes.len() as u64 <= PART_LIMIT,
            "DOCX part {name} is too large"
        );
        Ok(match bytes.as_slice() {
            [0xff, 0xfe, ..] | [b'<', 0, ..] => {
                let (text, _, invalid) = UTF_16LE.decode(&bytes);
                ensure!(!invalid, "Invalid UTF-16 in {name}");
                text.into_owned()
            }
            [0xfe, 0xff, ..] | [0, b'<', ..] => {
                let (text, _, invalid) = UTF_16BE.decode(&bytes);
                ensure!(!invalid, "Invalid UTF-16 in {name}");
                text.into_owned()
            }
            _ => String::from_utf8(bytes).with_context(|| format!("Invalid UTF-8 in {name}"))?,
        })
    }
}

fn escaped_bytes(uri: &str) -> impl Iterator<Item = Option<u8>> + '_ {
    uri.split('%').skip(1).map(|suffix| {
        let hex = suffix.get(..2)?;
        if !hex.bytes().all(|byte| byte.is_ascii_hexdigit()) {
            return None;
        }
        u8::from_str_radix(hex, 16).ok()
    })
}

pub(super) struct Relations<'a> {
    document: Document<'a>,
    owner: &'a str,
}

impl<'a> Relations<'a> {
    pub(super) fn name(owner: &str) -> String {
        match owner.rsplit_once('/') {
            Some((dir, name)) => format!("{dir}/_rels/{name}.rels"),
            None => format!("_rels/{owner}.rels"),
        }
    }

    pub(super) fn new(owner: &'a str, xml: &'a str) -> Result<Self> {
        Ok(Self {
            document: Document::parse(xml).context("Invalid DOCX relationships")?,
            owner,
        })
    }

    pub(super) fn part(&self, kind: &str) -> Result<Option<String>> {
        let Some(target) = self
            .document
            .root_element()
            .children()
            .filter(|node| node.has_tag_name((REL_NS, "Relationship")))
            .filter(|node| node.attribute("TargetMode") != Some("External"))
            .find(|node| {
                node.attribute("Type").is_some_and(|value| {
                    value.rsplit_once('/').is_some_and(|(prefix, local)| {
                        local == kind
                            && matches!(
                                prefix,
                                "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
                                    | "http://purl.oclc.org/ooxml/officeDocument/relationships"
                                    | "http://schemas.openxmlformats.org/package/2006/relationships/metadata"
                            )
                    })
                })
            })
            .and_then(|node| node.attribute("Target"))
        else {
            return Ok(None);
        };
        // Reject URI syntax before inspecting escapes: %3A, %3F and %23
        // are name data, not scheme, query or fragment delimiters.
        ensure!(
            !target.contains([':', '?', '#'])
                && !matches!(target.rsplit('/').next(), Some("" | "." | "..")),
            "Invalid DOCX relationship target"
        );
        let mut parts: Vec<&str> = match (target.starts_with('/'), self.owner.rsplit_once('/')) {
            (false, Some((parent, _))) => parent.split('/').collect(),
            _ => Vec::new(),
        };
        // Resolve against the relationship's owning part (not its _rels
        // directory). Only literal path delimiters and dot segments count.
        for part in target.strip_prefix('/').unwrap_or(target).split('/') {
            match part {
                "" => bail!("Invalid DOCX relationship target"),
                "." => (),
                ".." if parts.pop().is_none() => bail!("Invalid DOCX relationship target"),
                ".." => (),
                _ => {
                    // Inspect the decoded segment for validation only. Do
                    // not let an escape introduce a new path delimiter.
                    ensure!(
                        escaped_bytes(part).all(|byte| byte.is_some()),
                        "Invalid DOCX relationship target"
                    );
                    let decoded = percent_decode_str(part)
                        .decode_utf8()
                        .context("Invalid DOCX relationship target")?;
                    ensure!(
                        !decoded.contains(['/', '\\'])
                            && !decoded.chars().any(char::is_control)
                            && !matches!(decoded.as_ref(), "." | ".."),
                        "Invalid DOCX relationship target"
                    );
                    parts.push(part);
                }
            }
        }
        Ok(Some(parts.join("/")))
    }
}
