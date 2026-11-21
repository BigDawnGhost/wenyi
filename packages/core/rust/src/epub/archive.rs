//! Read and write container members without extracting them to the filesystem.

use std::{
    fs::File,
    io::{self, BufReader, BufWriter, Read, Seek, Write},
    path::Path,
};

use anyhow::{bail, ensure, Context, Result};
use flate2::{read::DeflateDecoder, CrcReader};
use zip::{read::ZipFile, write::SimpleFileOptions, CompressionMethod, ZipArchive, ZipWriter};

pub(crate) type Archive = ZipArchive<BufReader<File>>;

pub(crate) fn open(path: &Path) -> Result<Archive> {
    ZipArchive::new(BufReader::new(File::open(path)?)).context("Invalid EPUB ZIP archive")
}

pub(crate) fn read<R: Read + Seek>(zip: &mut ZipArchive<R>, href: &str) -> Result<Vec<u8>> {
    let mut data = Vec::new();
    zip.by_name(href)
        .with_context(|| format!("Missing EPUB member: {href}"))?
        .read_to_end(&mut data)?;
    Ok(data)
}

pub(crate) fn write_mimetype<W: Write + Seek>(out: &mut ZipWriter<W>, bytes: &[u8]) -> Result<()> {
    out.start_file(
        "mimetype",
        SimpleFileOptions::default().compression_method(CompressionMethod::Stored),
    )?;
    out.write_all(bytes)?;
    Ok(())
}

pub(super) fn write_text<W: Write + Seek>(
    out: &mut ZipWriter<W>,
    name: &str,
    content: &str,
) -> Result<()> {
    out.start_file(
        name,
        SimpleFileOptions::default()
            .compression_method(CompressionMethod::Deflated)
            .compression_level(Some(3)),
    )?;
    out.write_all(content.as_bytes())?;
    Ok(())
}

fn check_payload<R: Read>(reader: R, size: u64, crc: u32) -> io::Result<R> {
    let mut reader = CrcReader::new(reader);
    // Probe beyond the declared size so success requires reaching decoder EOF,
    // but do not inflate the rest of a stream once it exceeds that size.
    let actual = io::copy(
        &mut (&mut reader).take(size.saturating_add(1)),
        &mut io::sink(),
    )?;
    if actual != size {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!("Incorrect uncompressed size (expected {size}, read {actual})"),
        ));
    }
    if reader.crc().sum() != crc {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "Invalid checksum",
        ));
    }
    Ok(reader.into_inner())
}

fn verify_raw<R: Read>(file: ZipFile<'_, R>) -> Result<()> {
    let (size, compressed_size, crc) = (file.size(), file.compressed_size(), file.crc32());
    let consumed = match file.compression() {
        CompressionMethod::Stored => {
            check_payload(file, size, crc)?;
            size
        }
        CompressionMethod::Deflated => {
            check_payload(DeflateDecoder::new(file), size, crc)?.total_in()
        }
        method => bail!("Unsupported EPUB compression method: {method:?}"),
    };
    if consumed != compressed_size {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!("Incorrect compressed size (expected {compressed_size}, got {consumed})"),
        )
        .into());
    }
    Ok(())
}

/// Preserve the archive order, other than EPUB's mandatory first stored mimetype.
/// The callback supplies only changed content; other members are copied compressed and verified
/// in the caller-owned temporary file before publication.
pub(super) fn rewrite(
    zip: &mut Archive,
    destination: &Path,
    mut replacement: impl FnMut(&mut Archive, &str) -> Result<Option<String>>,
    extra: Option<(&str, &str)>,
) -> Result<()> {
    let mut out = ZipWriter::new(BufWriter::new(File::create(destination)?));
    let mimetype = zip.index_for_name("mimetype");
    if mimetype.is_some() {
        write_mimetype(&mut out, &read(zip, "mimetype")?)?;
    }
    let mut unchanged = Vec::new();
    for index in 0..zip.len() {
        if Some(index) == mimetype {
            continue;
        }
        let href = zip.name_for_index(index).unwrap_or_default().to_owned();
        let content = replacement(zip, &href)?;
        let file = zip.by_index_raw(index)?;
        if let Some(content) = content {
            let config = if file.compression() == CompressionMethod::Stored {
                file.options().compression_level(None)
            } else {
                file.options()
                    .compression_method(CompressionMethod::Deflated)
                    .compression_level(Some(3))
            };
            out.start_file(&href, config)?;
            out.write_all(content.as_bytes())?;
        } else {
            ensure!(!file.encrypted(), "Encrypted EPUB member: {href}");
            out.raw_copy_file(file)?;
            unchanged.push(href);
        }
    }
    if let Some((name, content)) = extra {
        write_text(&mut out, name, content)?;
    }
    out.finish()?.flush()?;
    if !unchanged.is_empty() {
        // Check the bytes we actually staged rather than rereading the source,
        // which can change between a verification pass and the raw copy.
        let mut staged = open(destination)?;
        for href in unchanged {
            let index = staged
                .index_for_name(&href)
                .with_context(|| format!("Missing staged EPUB member: {href}"))?;
            verify_raw(staged.by_index_raw(index)?)
                .with_context(|| format!("Invalid EPUB member: {href}"))?;
        }
    }
    Ok(())
}
