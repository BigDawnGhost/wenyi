//! Single-file HTML body export. Media lookup is exclusive to the EPUB archive.

use std::{collections::HashMap, path::Path};

use anyhow::Result;
use base64::{engine::general_purpose::STANDARD, Engine};

use super::{
    archive::{open, read, Archive},
    dom::{decode, strip, Dom, Id},
    export::{resource_pages, Options},
    links::media_path,
};

pub(crate) fn bodies(
    path: &Path,
    mut options: Options,
    mime_types: &HashMap<String, String>,
) -> Result<(Vec<String>, Vec<String>)> {
    let mut zip = open(path)?;
    let (mut rendered, warnings) = resource_pages(&mut zip, &mut options, |dom, _| dom)?;
    let mut media = Media {
        zip: &mut zip,
        mime_types,
        urls: HashMap::new(),
    };
    let mut output = Vec::with_capacity(options.resources.len());
    for spec in &options.resources {
        let mut dom = if let Some(dom) = rendered.shift_remove(&spec.href) {
            dom
        } else {
            if media.zip.index_for_name(&spec.href).is_none() {
                continue;
            }
            Dom::parse(&decode(&read(media.zip, &spec.href)?))
        };
        // Image-only spine resources remain visible even without saved segments.
        let body = dom.find(0, "body").unwrap_or(0);
        media.embed(&mut dom, body, &spec.href)?;
        output.push(dom.inner_html(body));
    }
    Ok((output, warnings))
}

struct Media<'a> {
    zip: &'a mut Archive,
    mime_types: &'a HashMap<String, String>,
    urls: HashMap<String, String>,
}

impl Media<'_> {
    fn inline(&mut self, base: &str, original: &str) -> Result<Option<&str>> {
        let reference = strip(original);
        if reference.is_empty() || reference.starts_with('#') {
            return Ok(None);
        }
        let relative = media_path(base, reference);
        if relative.external {
            // Includes data:, file:, network and scheme-relative references.
            return Ok(None);
        }
        let root = media_path("", reference);
        let candidate = [relative.path, root.path].into_iter().find_map(|path| {
            let extension = path.rsplit('/').next()?.rsplit_once('.')?.1;
            let mime = self.mime_types.get(&extension.to_ascii_lowercase())?;
            self.zip.index_for_name(&path)?;
            Some((path, mime))
        });
        let Some((path, mime)) = candidate else {
            return Ok(None);
        };
        if !self.urls.contains_key(&path) {
            let bytes = read(self.zip, &path)?;
            self.urls.insert(
                path.clone(),
                format!("data:{mime};base64,{}", STANDARD.encode(bytes)),
            );
        }
        Ok(self.urls.get(&path).map(String::as_str))
    }

    fn embed(&mut self, dom: &mut Dom, root: Id, base: &str) -> Result<()> {
        let nodes: Vec<_> = dom.tags(root).collect();
        for node in nodes {
            let attrs: &[&str] = match dom.name(node) {
                "img" | "source" | "embed" | "audio" => &["src"],
                "object" => &["data"],
                "video" => &["src", "poster"],
                "image" => &["href", "xlink:href"],
                _ => continue,
            };
            for attr in attrs {
                if let Some(value) = dom.attr(node, attr) {
                    if let Some(url) = self.inline(base, value)? {
                        dom.set(node, attr, url);
                    }
                }
            }
            if let Some(value) = dom.attr(node, "srcset") {
                let mut rewritten = String::new();
                for (reference, descriptor) in srcset(value) {
                    if !rewritten.is_empty() {
                        rewritten.push_str(", ");
                    }
                    rewritten.push_str(self.inline(base, reference)?.unwrap_or(reference));
                    if !descriptor.is_empty() {
                        rewritten.push(' ');
                        rewritten.push_str(descriptor);
                    }
                }
                dom.set(node, "srcset", rewritten);
            }
        }
        Ok(())
    }
}

// Read URL/descriptor pairs. Commas inside a data URL belong to the URL.
pub(crate) fn srcset(mut value: &str) -> impl Iterator<Item = (&str, &str)> {
    fn space(c: char) -> bool {
        matches!(c, '\t' | '\n' | '\x0c' | '\r' | ' ')
    }
    std::iter::from_fn(move || {
        value = value.trim_start_matches(|c| space(c) || c == ',');
        if value.is_empty() {
            return None;
        }
        let end = value.find(space).unwrap_or(value.len());
        let (reference, remaining) = value.split_at(end);
        if reference.ends_with(',') {
            value = remaining;
            return Some((reference.trim_end_matches(','), ""));
        }
        let (descriptor, next) = remaining.split_once(',').unwrap_or((remaining, ""));
        value = next;
        Some((reference, descriptor.trim_matches(space)))
    })
}

#[cfg(test)]
mod tests {
    use super::srcset;

    #[test]
    fn srcset_preserves_commas_inside_data_uris() {
        assert_eq!(
            srcset("data:image/png;base64,AAAA 1x, cover.png 2x, fallback.png, x.png")
                .collect::<Vec<_>>(),
            [
                ("data:image/png;base64,AAAA", "1x"),
                ("cover.png", "2x"),
                ("fallback.png", ""),
                ("x.png", ""),
            ],
        );
    }
}
