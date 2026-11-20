use anyhow::{ensure, Context, Result};
use lopdf::{Document, Object, ObjectId};

use super::inherited;

pub(super) fn number(object: &Object) -> Result<f64> {
    let value = match object {
        Object::Integer(value) => *value as f64,
        Object::Real(value) => f64::from(*value),
        _ => anyhow::bail!("expected a PDF number"),
    };
    ensure!(value.is_finite(), "nonfinite PDF number");
    Ok(value)
}

pub(super) fn real(value: f64) -> Result<Object> {
    let value = value as f32;
    ensure!(value.is_finite(), "coordinate exceeds PDF number range");
    Ok(Object::Real(value))
}

pub(super) fn rectangle(coords: [f64; 4]) -> Result<Object> {
    Ok(Object::Array(
        coords.map(real).into_iter().collect::<Result<_>>()?,
    ))
}

pub(super) fn read_rect(doc: &Document, object: &Object) -> Result<[f64; 4]> {
    let coords: &[Object; 4] = object.as_array()?.as_slice().try_into()?;
    let [x0, y0, x1, y1] = coords
        .each_ref()
        .map(|value| doc.dereference(value).map(|pair| pair.1));
    Ok([number(x0?)?, number(y0?)?, number(x1?)?, number(y1?)?])
}

#[derive(Clone, Copy)]
pub(super) struct Placement {
    pub(super) matrix: [f64; 6],
    pub(super) crop: [f64; 4],
}

impl Placement {
    pub(super) fn apply(self, x: f64, y: f64) -> (f64, f64) {
        let [a, b, c, d, e, f] = self.matrix;
        (a * x + c * y + e, b * x + d * y + f)
    }

    pub(super) fn transform_rect(self, [x0, y0, x1, y1]: [f64; 4]) -> [f64; 4] {
        let points = [
            self.apply(x0, y0),
            self.apply(x0, y1),
            self.apply(x1, y0),
            self.apply(x1, y1),
        ];
        points.into_iter().fold(
            [
                f64::INFINITY,
                f64::INFINITY,
                f64::NEG_INFINITY,
                f64::NEG_INFINITY,
            ],
            |[left, bottom, right, top], (x, y)| {
                [left.min(x), bottom.min(y), right.max(x), top.max(y)]
            },
        )
    }
}

pub(super) struct Layout {
    pub(super) width: f64,
    pub(super) height: f64,
    normalized: Placement,
}

impl Layout {
    pub(super) fn from_page(doc: &Document, page: ObjectId) -> Result<Self> {
        let crop = match inherited(doc, page, b"CropBox")? {
            Some(crop) => crop,
            None => inherited(doc, page, b"MediaBox")?.context("PDF page has no media box")?,
        };
        let crop = read_rect(doc, crop)?;
        let unit = doc
            .get_dictionary(page)?
            .get(b"UserUnit")
            .ok()
            .map(|raw| number(doc.dereference(raw)?.1))
            .transpose()?
            .unwrap_or(1.);
        let rotation = inherited(doc, page, b"Rotate")?
            .map(Object::as_i64)
            .transpose()?
            .unwrap_or(0);
        let matrix = match rotation.rem_euclid(360) {
            0 => [unit, 0., 0., unit, 0., 0.],
            90 => [0., -unit, unit, 0., 0., 0.],
            180 => [-unit, 0., 0., -unit, 0., 0.],
            270 => [0., unit, -unit, 0., 0., 0.],
            _ => anyhow::bail!("PDF rotation is not a multiple of 90"),
        };
        let mut normalized = Placement { matrix, crop };
        let [left, bottom, right, top] = normalized.transform_rect(crop);
        let (width, height) = (right - left, top - bottom);
        ensure!(
            crop[0] < crop[2] && crop[1] < crop[3] && unit > 0. && width > 0. && height > 0.,
            "invalid visible PDF dimensions"
        );
        normalized.matrix[4] = -left;
        normalized.matrix[5] = -bottom;
        Ok(Self {
            width,
            height,
            normalized,
        })
    }

    pub(super) fn place(&self, x: f64, spread_height: f64) -> Placement {
        let mut placement = self.normalized;
        let y = spread_height - self.height;
        placement.matrix[4] += x;
        placement.matrix[5] += y;
        placement
    }
}
