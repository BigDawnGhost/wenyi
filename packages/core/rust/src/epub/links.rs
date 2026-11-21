//! EPUB-relative ZIP paths and fragment identities.

use anyhow::{bail, Result};
use percent_encoding::percent_decode_str;

pub(super) struct Link {
    pub path: String,
    pub fragment: String,
    pub external: bool,
}

impl Link {
    pub fn key(&self) -> String {
        if self.path.is_empty() || self.fragment.is_empty() {
            self.path.clone()
        } else {
            format!("{}#{}", self.path, self.fragment)
        }
    }
}

pub(super) fn resolve(base: &str, href: &str) -> Link {
    resolve_in(base, href, false)
}

/// The HTML exporter historically preserves a leading slash in ZIP member lookups.
pub(super) fn media_path(base: &str, href: &str) -> Link {
    resolve_in(base, href, true)
}

fn resolve_in(base: &str, href: &str, preserve_root: bool) -> Link {
    let href = href.trim_start_matches(|c| c <= ' ');
    let cleaned;
    let href = if href.contains(['\r', '\n', '\t']) {
        cleaned = href.replace(['\r', '\n', '\t'], "");
        cleaned.as_str()
    } else {
        href
    };
    let (target, fragment) = href.split_once('#').unwrap_or((href, ""));
    let fragment = percent_decode_str(fragment)
        .decode_utf8_lossy()
        .into_owned();
    let path = target.split('?').next().unwrap_or("");
    let external = path.starts_with("//") && path[2..].chars().next().is_some_and(|c| c != '/')
        || path.split_once(':').is_some_and(|(scheme, _)| {
            scheme
                .as_bytes()
                .first()
                .is_some_and(u8::is_ascii_alphabetic)
                && scheme
                    .bytes()
                    .all(|c| c.is_ascii_alphanumeric() || b"+-.".contains(&c))
        });
    if external {
        return Link {
            path: String::new(),
            fragment,
            external,
        };
    }
    let path = percent_decode_str(path).decode_utf8_lossy();
    let full = if path.starts_with('/') {
        if preserve_root {
            path.into_owned()
        } else {
            path.trim_start_matches('/').to_owned()
        }
    } else if path.is_empty() {
        base.to_owned()
    } else if let Some((dir, _)) = base.rsplit_once('/') {
        format!("{dir}/{path}")
    } else {
        path.into_owned()
    };
    let absolute = preserve_root && full.starts_with('/');
    let mut parts: Vec<&str> = Vec::new();
    for part in full.split('/') {
        match part {
            "" | "." => {}
            ".." if parts.last().is_some_and(|s| *s != "..") => {
                parts.pop();
            }
            ".." if absolute => {}
            _ => parts.push(part),
        }
    }
    let mut path = parts.join("/");
    if absolute {
        path.insert(0, '/');
    }
    Link {
        path,
        fragment,
        external,
    }
}

pub(super) fn is_html(path: &str) -> bool {
    [".xhtml", ".html", ".htm"]
        .iter()
        .any(|ext| path.ends_with(ext))
}

pub(super) fn validate_path(path: &str) -> Result<()> {
    if path.contains(['\\', ':', '?', '#', '\0'])
        || path.split('/').any(|part| matches!(part, "" | "." | ".."))
    {
        bail!("Invalid EPUB resource path: {path:?}");
    }
    Ok(())
}
