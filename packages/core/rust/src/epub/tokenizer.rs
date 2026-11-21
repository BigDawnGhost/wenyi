//! Fast XHTML tokenizer and bounded encoding declaration probes.

use std::{ops::Range, sync::LazyLock};

use encoding_rs::{Encoding, UTF_16BE, UTF_16LE, UTF_8};
use html5ever::{
    tendril::StrTendril,
    tokenizer::{
        states::RawKind, BufferQueue, StartTag, Token as HtmlToken, TokenSink, TokenSinkResult,
        Tokenizer,
    },
    TokenizerResult,
};
use quick_xml::{events::Event, Reader};
use regex::{bytes::Regex as ByteRegex, Regex};

use super::dom::void;

static XML_DECL: LazyLock<ByteRegex> = LazyLock::new(|| {
    ByteRegex::new(concat!(
        r#"\A<\?xml[ \t\r\n]+version[ \t\r\n]*=[ \t\r\n]*(?:"1\.[0-9]+"|'1\.[0-9]+')"#,
        r#"[ \t\r\n]+encoding[ \t\r\n]*=[ \t\r\n]*(?:"([A-Za-z][A-Za-z0-9_.-]*)"|'([A-Za-z][A-Za-z0-9_.-]*)')"#,
        r#"(?:[ \t\r\n]+standalone[ \t\r\n]*=[ \t\r\n]*(?:"(?:yes|no)"|'(?:yes|no)'))?[ \t\r\n]*\?>"#,
    ))
    .unwrap()
});

// Only an XML declaration can set the XML encoding. In particular, an
// xml-stylesheet PI or something later in the document cannot set it.
pub(super) fn xml_encoding(bytes: &[u8]) -> Option<Range<usize>> {
    if !bytes.starts_with(b"<?xml") {
        return None;
    }
    let groups = XML_DECL.captures(bytes)?;
    Some(groups.get(1).or_else(|| groups.get(2))?.range())
}

static MIME_PARAM: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r#"(?:\A|;)[ \t\r\n\x0c]*([^ \t\r\n\x0c;=]+)[ \t\r\n\x0c]*=[ \t\r\n\x0c]*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)'|([^ \t\r\n\x0c;"']+))"#,
    )
    .unwrap()
});

pub(super) fn pragma_charset<'a>(
    http_equiv: Option<&str>,
    content: &'a str,
) -> Option<(Range<usize>, &'a str)> {
    if !http_equiv?
        .trim_ascii()
        .eq_ignore_ascii_case("content-type")
    {
        return None;
    }
    // Consume whole parameters, so e.g. a quoted profile containing
    // "; charset=..." does not mask the real charset or get rewritten.
    for groups in MIME_PARAM.captures_iter(content) {
        if groups[1].eq_ignore_ascii_case("charset") {
            let m = groups
                .get(2)
                .or_else(|| groups.get(3))
                .or_else(|| groups.get(4))?;
            let label = m.as_str().trim_ascii();
            let start = m.start() + m.as_str().len() - m.as_str().trim_ascii_start().len();
            return Some((start..start + label.len(), label));
        }
    }
    None
}

struct MetaEncoding;

impl TokenSink for MetaEncoding {
    type Handle = ();

    fn adjusted_current_node_present_but_not_in_html_namespace(&self) -> bool {
        // EPUB XHTML can contain CDATA in HTML elements.
        true
    }

    fn process_token(&self, token: HtmlToken, _: u64) -> TokenSinkResult<()> {
        if let HtmlToken::TagToken(tag) = token {
            if tag.kind == StartTag {
                let name = tag.name.as_ref();
                if name == "meta" {
                    let attr = |key: &str| {
                        tag.attrs
                            .iter()
                            .find(|a| a.name.local.as_ref() == key)
                            .map(|a| a.value.as_ref())
                    };
                    let encoding = attr("charset")
                        .and_then(|s| Encoding::for_label(s.as_bytes()))
                        .or_else(|| {
                            let (_, label) = pragma_charset(attr("http-equiv"), attr("content")?)?;
                            Encoding::for_label(label.as_bytes())
                        });
                    if let Some(encoding) = encoding {
                        // An ASCII-compatible HTML meta tag cannot declare UTF-16.
                        let encoding = if encoding == UTF_16LE || encoding == UTF_16BE {
                            UTF_8
                        } else {
                            encoding
                        };
                        return TokenSinkResult::EncodingIndicator(encoding.name().into());
                    }
                } else if !tag.self_closing {
                    return match name {
                        "script" => TokenSinkResult::RawData(RawKind::ScriptData),
                        "title" | "textarea" => TokenSinkResult::RawData(RawKind::Rcdata),
                        "style" | "xmp" | "iframe" | "noembed" | "noframes" => {
                            TokenSinkResult::RawData(RawKind::Rawtext)
                        }
                        "plaintext" => TokenSinkResult::Plaintext,
                        _ => TokenSinkResult::Continue,
                    };
                }
            }
        }
        TokenSinkResult::Continue
    }
}

pub(super) fn html_encoding(bytes: &[u8]) -> Option<&'static Encoding> {
    let prefix = &bytes[..bytes.len().min(1024)];
    // Most pages need no HTML probe at all. Tokenize only the bounded prefix
    // of candidates, not the whole document, and only act on real start tags.
    if !prefix.windows(5).any(|s| s.eq_ignore_ascii_case(b"<meta")) {
        return None;
    }
    let tokenizer = Tokenizer::new(MetaEncoding, Default::default());
    let input = BufferQueue::default();
    input.push_back(StrTendril::from(String::from_utf8_lossy(prefix).as_ref()));
    match tokenizer.feed(&input) {
        TokenizerResult::EncodingIndicator(label) => Encoding::for_label(label.as_bytes()),
        _ => None,
    }
}

type Attributes<'a> = Vec<(&'a str, String)>;
pub(crate) type Token<'a> = (u8, &'a str, Option<Attributes<'a>>);

fn name(bytes: &[u8]) -> Option<&str> {
    // HTMLParser folds ASCII case and accepts a wider grammar. Leave such input
    // to the tolerant parser rather than interpreting it as strict XHTML.
    if !bytes.first()?.is_ascii_lowercase()
        || !bytes.iter().all(|b| {
            b.is_ascii_lowercase() || b.is_ascii_digit() || matches!(b, b'-' | b':' | b'_' | b'.')
        })
    {
        return None;
    }
    std::str::from_utf8(bytes).ok()
}

fn entity(name: &str) -> Option<&'static str> {
    match name {
        "amp" => Some("&"),
        "lt" => Some("<"),
        "gt" => Some(">"),
        "quot" => Some("\""),
        "apos" => Some("'"),
        _ => None,
    }
}

fn attribute_value(value: &str) -> Option<String> {
    let mut parts = value.split('&');
    let mut decoded = parts.next()?.to_owned();
    for part in parts {
        let (reference, tail) = part.split_once(';')?;
        decoded.push_str(entity(reference)?);
        decoded.push_str(tail);
    }
    Some(decoded)
}

pub(super) fn scan(input: &str) -> Option<Vec<Token<'_>>> {
    if input.starts_with('\u{feff}') || input.contains('\0') {
        return None;
    }
    let mut reader = Reader::from_str(input);
    let mut tokens = Vec::new();
    let mut depth = 0usize;
    loop {
        let start = reader.buffer_position() as usize;
        let event = reader.read_event().ok()?;
        let raw = input.get(start..reader.buffer_position() as usize)?;
        let token = match event {
            Event::Start(_) | Event::Empty(_) => {
                let empty = matches!(event, Event::Empty(_));
                let body = raw
                    .strip_prefix('<')?
                    .strip_suffix(if empty { "/>" } else { ">" })?;
                let tag = name(body.split_ascii_whitespace().next()?.as_bytes())?;
                if matches!(
                    tag,
                    "script"
                        | "style"
                        | "textarea"
                        | "xmp"
                        | "plaintext"
                        | "iframe"
                        | "noembed"
                        | "noframes"
                        | "noscript"
                ) || (void(tag) && !empty)
                {
                    return None;
                }
                depth += usize::from(!empty);
                let mut attrs = Vec::new();
                let mut tail = &body[tag.len()..];
                while !tail.is_empty() {
                    if !tail.as_bytes()[0].is_ascii_whitespace() {
                        return None;
                    }
                    tail = tail.trim_start_matches(|c: char| c.is_ascii_whitespace());
                    if tail.is_empty() {
                        break;
                    }
                    let (key, rest) = tail.split_once('=')?;
                    let key = name(
                        key.trim_end_matches(|c: char| c.is_ascii_whitespace())
                            .as_bytes(),
                    )?;
                    let rest = rest.trim_start_matches(|c: char| c.is_ascii_whitespace());
                    let quote = rest.chars().next()?;
                    if !matches!(quote, '\'' | '"') {
                        return None;
                    }
                    let (value, rest) = rest[1..].split_once(quote)?;
                    if value.contains('<') {
                        return None;
                    }
                    attrs.push((key, attribute_value(value)?));
                    tail = rest;
                }
                (if empty { 2 } else { 1 }, tag, Some(attrs))
            }
            Event::End(_) => {
                let tag = raw
                    .strip_prefix("</")?
                    .strip_suffix('>')?
                    .trim_end_matches(|c: char| c.is_ascii_whitespace());
                name(tag.as_bytes())?;
                depth = depth.checked_sub(1)?;
                (3, tag, None)
            }
            Event::Text(_) => (0, raw, None),
            Event::GeneralRef(_) => (0, entity(raw.strip_prefix('&')?.strip_suffix(';')?)?, None),
            Event::CData(_) => (7, raw.strip_prefix("<![CDATA[")?.strip_suffix("]]>")?, None),
            Event::Comment(_) => {
                let text = raw.strip_prefix("<!--")?.strip_suffix("-->")?;
                if text.contains("--") || text.ends_with('-') {
                    return None;
                }
                (4, text, None)
            }
            Event::DocType(_) => {
                let text = raw.strip_prefix("<!DOCTYPE ")?.strip_suffix('>')?;
                if text.contains(['[', ']', '<', '>']) {
                    return None;
                }
                (5, text, None)
            }
            Event::Decl(_) | Event::PI(_) => {
                let text = raw.strip_prefix("<?")?.strip_suffix('>')?;
                if text.contains('>') {
                    return None;
                }
                (6, text, None)
            }
            Event::Eof => return (depth == 0).then_some(tokens),
        };
        tokens.push(token);
    }
}
