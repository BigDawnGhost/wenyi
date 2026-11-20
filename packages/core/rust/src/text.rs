//! Plain-text and Markdown structure. Decoding policy and storage stay in Python.

use std::{
    fs::File,
    io::{self, BufWriter, Write},
    path::Path,
    sync::LazyLock,
};

use regex::Regex;

// Python str.isspace() includes U+001C..U+001F in addition to Unicode White_Space.
const SPACE: &str = r"[\s\x1c-\x1f]";
static HEADING: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(&format!(r"^(#{{1,6}}){SPACE}+(.*[^\s\x1c-\x1f]){SPACE}*$")).unwrap()
});
static JAPANESE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(&format!(
        r"^{SPACE}*(?:第[0-9０-９一二三四五六七八九十百千]+[章話节節回部巻]|序章|終章|序幕|終幕|プロローグ|エピローグ|あとがき|まえがき)"
    )).unwrap()
});
static LINE_BREAK: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\r\n|[\n\r\x0b\x0c\x1c-\x1e\x85\u2028\u2029]").unwrap());
static PARAGRAPH_BREAK: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(&format!(r"\n{SPACE}*\n")).unwrap());
static RUBY: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"〘[^〙]*〙").unwrap());

fn strip(value: &str) -> &str {
    value.trim_matches(|c: char| c.is_whitespace() || matches!(c, '\x1c'..='\x1f'))
}

pub(crate) type Parsed = (Option<(String, usize)>, Vec<String>);
pub(crate) type Rendered = Vec<Vec<(String, String, String, usize)>>;

pub(crate) fn parse(content: &str) -> Vec<Parsed> {
    let mut result = Vec::new();
    let mut title = None;
    let mut body = Vec::new();
    let flush = |output: &mut Vec<Parsed>, title: Option<(String, usize)>, body: &mut Vec<&str>| {
        if title.is_none() && body.is_empty() {
            return;
        }
        let block = body.join("\n");
        body.clear();
        let paragraphs = PARAGRAPH_BREAK
            .split(&block)
            .filter(|p| !strip(p).is_empty())
            .map(|p| p.trim_matches('\n').to_owned())
            .collect();
        output.push((title, paragraphs));
    };
    let mut lines = LINE_BREAK.split(content).peekable();
    while let Some(line) = lines.next() {
        if line.is_empty() && lines.peek().is_none() {
            break;
        }
        let heading = HEADING
            .captures(line)
            .and_then(|m| {
                let text = strip(m.get(2)?.as_str());
                Some((text.to_owned(), m[1].len()))
            })
            .or_else(|| JAPANESE.is_match(line).then(|| (strip(line).to_owned(), 1)));
        if let Some(heading) = heading {
            flush(&mut result, title.take(), &mut body);
            title = Some(heading);
        } else {
            body.push(line);
        }
    }
    flush(&mut result, title, &mut body);
    result
}

pub(crate) fn write(
    path: &Path,
    chapters: Rendered,
    bilingual: bool,
    source_first: bool,
    markdown: bool,
) -> io::Result<()> {
    let mut out = BufWriter::new(File::create(path)?);
    for (index, chapter) in chapters.into_iter().enumerate() {
        if index > 0 {
            write_text(&mut out, "\n\n")?;
        }
        let mut first = true;
        let mut block = |text: &str| {
            if !first {
                write_text(&mut out, "\n\n")?;
            }
            first = false;
            write_text(&mut out, text)
        };
        for (kind, mut target, source, level) in chapter {
            if kind == "heading" {
                if markdown {
                    target = format!("{} {target}", "#".repeat(level.min(6)));
                }
                block(&target)?;
                continue;
            }
            if bilingual && source != target {
                let plain = RUBY.replace_all(&source, |m: &regex::Captures<'_>| {
                    if m[0].starts_with("〘object:") {
                        m[0].to_owned()
                    } else {
                        String::new()
                    }
                });
                if !strip(&plain).is_empty() && plain != target {
                    if source_first {
                        block(&plain)?;
                        block(&target)?;
                    } else {
                        block(&target)?;
                        block(&plain)?;
                    }
                    continue;
                }
            }
            block(&target)?;
        }
    }
    write_text(&mut out, "\n")?;
    out.flush()
}

fn write_text(out: &mut impl Write, text: &str) -> io::Result<()> {
    // Match the legacy Python text-mode newline translation on Windows.
    if cfg!(windows) {
        out.write_all(text.replace('\n', "\r\n").as_bytes())
    } else {
        out.write_all(text.as_bytes())
    }
}
