use std::collections::{HashMap, HashSet};

use anyhow::{ensure, Result};
use lopdf::{decode_text_string, Dictionary, Document, Object, ObjectId};

use super::{
    geometry::{number, read_rect, real, rectangle},
    CopyObjects, Destinations, Input,
};

pub(super) struct NamedDestinations(HashMap<String, Object>);

impl NamedDestinations {
    pub(super) fn load(doc: &Document) -> Result<Self> {
        let mut entries = HashMap::new();
        let catalog = doc.catalog()?;
        let root = if let Ok(legacy) = catalog.get(b"Dests") {
            Some(legacy)
        } else if let Ok(names) = catalog.get(b"Names") {
            doc.dereference(names)?.1.as_dict()?.get(b"Dests").ok()
        } else {
            None
        };
        let mut stack: Vec<_> = root.into_iter().collect();
        let mut visited = HashSet::new();
        while let Some(node) = stack.pop() {
            let (id, node) = doc.dereference(node)?;
            if let Some(id) = id {
                ensure!(
                    visited.insert(id),
                    "repeated node in PDF destination name tree"
                );
            }
            if matches!(node, Object::Null) {
                continue;
            }
            let node = node.as_dict()?;
            if let Ok(kids) = node.get(b"Kids") {
                stack.extend(doc.dereference(kids)?.1.as_array()?.iter().rev());
            } else if let Ok(names) = node.get(b"Names") {
                let names = doc.dereference(names)?.1.as_array()?;
                ensure!(names.len() % 2 == 0, "unpaired PDF destination name");
                for pair in names.chunks_exact(2) {
                    let key = name(doc.dereference(&pair[0])?.1)?;
                    entries.insert(key, pair[1].clone());
                }
            } else {
                for (key, value) in node {
                    entries.insert(std::str::from_utf8(key)?.to_owned(), value.clone());
                }
            }
        }
        Ok(Self(entries))
    }

    fn resolve<'a>(&'a self, doc: &'a Document, raw: &'a Object) -> Result<Option<&'a Object>> {
        let raw = doc.dereference(raw)?.1;
        if !matches!(raw, Object::Name(_) | Object::String(..)) {
            return Ok(Some(raw));
        }
        let key = name(raw)?;
        let Some(named) = self
            .0
            .get(&key)
            .or_else(|| self.0.get(key.trim_start_matches('/')))
        else {
            return Ok(None);
        };
        let named = doc.dereference(named)?.1;
        match named {
            Object::Dictionary(dict) => Ok(Some(dict.get_deref(b"D", doc)?)),
            named => Ok(Some(named)),
        }
    }
}

fn name(object: &Object) -> Result<String> {
    match object {
        Object::Name(bytes) => Ok(std::str::from_utf8(bytes)?.to_owned()),
        _ => Ok(decode_text_string(object)?),
    }
}

fn destination(
    doc: &Document,
    raw: &Object,
    names: &NamedDestinations,
    destinations: &Destinations,
    pages: &[ObjectId],
) -> Result<Option<Object>> {
    let Some(Object::Array(values)) = names.resolve(doc, raw)? else {
        return Ok(None);
    };
    let [page, view, coordinates @ ..] = values.as_slice() else {
        return Ok(None);
    };
    let page = match page {
        Object::Integer(index) => usize::try_from(*index)
            .ok()
            .and_then(|index| pages.get(index))
            .copied(),
        Object::Reference(_) => doc.dereference(page)?.0,
        _ => None,
    };
    let Some(&(spread, place)) = page.and_then(|id| destinations.get(&id)) else {
        // A link to a source page omitted from the export must not survive.
        return Ok(None);
    };
    let coordinate = |index: usize| -> Result<Option<f64>> {
        let Some(value) = coordinates.get(index) else {
            return Ok(None);
        };
        match doc.dereference(value)?.1 {
            value @ (Object::Integer(_) | Object::Real(_)) => Ok(Some(number(value)?)),
            _ => Ok(None),
        }
    };
    let view = doc.dereference(view)?.1.as_name()?;
    let [left, _, _, top] = place.crop;
    let mut mapped = vec![Object::Reference(spread)];
    match view {
        b"XYZ" | b"FitH" | b"FitBH" | b"FitV" | b"FitBV" => {
            let (x, y, zoom) = match view {
                b"XYZ" => (
                    coordinate(0)?.unwrap_or(left),
                    coordinate(1)?.unwrap_or(top),
                    match coordinates.get(2) {
                        Some(value) => doc.dereference(value)?.1.clone(),
                        None => Object::Null,
                    },
                ),
                b"FitH" | b"FitBH" => (left, coordinate(0)?.unwrap_or(top), Object::Null),
                _ => (coordinate(0)?.unwrap_or(left), top, Object::Null),
            };
            ensure!(
                matches!(zoom, Object::Null | Object::Integer(_) | Object::Real(_)),
                "unsupported PDF destination zoom"
            );
            let (x, y) = place.apply(x, y);
            mapped.extend([Object::Name(b"XYZ".to_vec()), real(x)?, real(y)?, zoom]);
        }
        b"FitR" if coordinates.len() == 4 => {
            let [Some(x0), Some(y0), Some(x1), Some(y1)] = [
                coordinate(0)?,
                coordinate(1)?,
                coordinate(2)?,
                coordinate(3)?,
            ] else {
                return Ok(None);
            };
            mapped.push(Object::Name(b"FitR".to_vec()));
            for value in place.transform_rect([x0, y0, x1, y1]) {
                mapped.push(real(value)?);
            }
        }
        b"Fit" | b"FitB" | b"FitR" => mapped.push(Object::Name(b"Fit".to_vec())),
        _ => anyhow::bail!("unsupported PDF destination view"),
    }
    Ok(Some(Object::Array(mapped)))
}

pub(super) fn append(
    output: &mut Document,
    copier: &mut CopyObjects<'_>,
    input: &Input,
    page_id: ObjectId,
    destinations: &Destinations,
    annotations: &mut Vec<Object>,
) -> Result<()> {
    let doc = copier.input;
    let page = doc.get_dictionary(page_id)?;
    let Ok(raw) = page.get(b"Annots") else {
        return Ok(());
    };
    let raw = doc.dereference(raw)?.1;
    if matches!(raw, Object::Null) {
        return Ok(());
    }
    let (spread, place) = destinations[&page_id];
    for reference in raw.as_array()? {
        let original = doc.dereference(reference)?.1.as_dict()?;
        ensure!(
            matches!(
                original
                    .get_deref(b"Subtype", doc)
                    .and_then(Object::as_name),
                Ok(b"Link")
            ),
            "non-link PDF annotation needs the compatibility writer"
        );
        let mut cloned = original.clone();
        let raw_dest = if let Ok(direct) = original.get(b"Dest") {
            cloned.remove(b"Dest");
            Some(direct)
        } else if let Some(action) = goto_action(doc, original)? {
            cloned.remove(b"A");
            action.get(b"D").ok()
        } else {
            None
        };
        let mapped = if let Some(raw_dest) = raw_dest {
            let Some(mapped) =
                destination(doc, raw_dest, &input.names, destinations, &input.pages)?
            else {
                continue;
            };
            Some(mapped)
        } else {
            None
        };
        for key in [
            b"P".as_slice(),
            b"StructParent",
            b"Parent",
            b"Rect",
            b"QuadPoints",
        ] {
            cloned.remove(key);
        }
        let mut annotation = copier.copy_dict(output, &cloned, 0)?;
        annotation.set("P", spread);
        let rect = read_rect(doc, original.get_deref(b"Rect", doc)?)?;
        annotation.set("Rect", rectangle(place.transform_rect(rect))?);
        if let Ok(quads) = original.get(b"QuadPoints") {
            let quads = doc.dereference(quads)?.1.as_array()?;
            ensure!(quads.len() % 2 == 0, "unpaired PDF link quad coordinates");
            let mut transformed = Vec::with_capacity(quads.len());
            for pair in quads.chunks_exact(2) {
                let x = number(doc.dereference(&pair[0])?.1)?;
                let y = number(doc.dereference(&pair[1])?.1)?;
                let (x, y) = place.apply(x, y);
                transformed.extend([real(x)?, real(y)?]);
            }
            annotation.set("QuadPoints", transformed);
        }
        if let Some(mapped) = mapped {
            annotation.set("Dest", mapped);
        }
        annotations.push(Object::Reference(output.add_object(annotation)));
    }
    Ok(())
}

fn goto_action<'a>(
    doc: &'a Document,
    annotation: &'a Dictionary,
) -> Result<Option<&'a Dictionary>> {
    let Ok(raw) = annotation.get(b"A") else {
        return Ok(None);
    };
    let action = doc.dereference(raw)?.1;
    let Object::Dictionary(action) = action else {
        return Ok(None);
    };
    Ok(matches!(
        action.get_deref(b"S", doc).and_then(Object::as_name),
        Ok(b"GoTo")
    )
    .then_some(action))
}
