//! EPUB markup arena. Mutations use stable node indices rather than serialized templates.

use std::{
    borrow::Cow,
    cell::{Cell, RefCell},
    collections::BTreeMap,
};

use chardetng::{EncodingDetector, Iso2022JpDetection, Utf8Detection};
use encoding_rs::{Encoding, UTF_16BE, UTF_16LE, WINDOWS_1252};
use html5ever::{
    tendril::StrTendril,
    tokenizer::{states::RawKind, BufferQueue, Token, TokenSink, TokenSinkResult, Tokenizer},
};

use super::tokenizer::{html_encoding, pragma_charset, xml_encoding};

pub type Id = usize;
pub type Attrs = BTreeMap<String, String>;

#[derive(Clone)]
pub enum Kind {
    Tag(String, Attrs),
    Text(String),
    Raw(String),
    Pi(String),
}

#[derive(Clone)]
struct Node {
    kind: Kind,
    parent: Option<Id>,
    children: Vec<Id>,
}

#[derive(Clone)]
pub struct Dom(Vec<Node>);

pub fn strip(value: &str) -> &str {
    value.trim_matches(|c: char| c.is_whitespace() || matches!(c, '\x1c'..='\x1f'))
}

pub fn escape(out: &mut String, value: &str, attr: bool) {
    for c in value.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' if attr => out.push_str("&quot;"),
            _ => out.push(c),
        }
    }
}

pub fn void(name: &str) -> bool {
    matches!(
        name,
        "area"
            | "base"
            | "basefont"
            | "bgsound"
            | "br"
            | "col"
            | "command"
            | "embed"
            | "frame"
            | "hr"
            | "img"
            | "input"
            | "isindex"
            | "keygen"
            | "link"
            | "menuitem"
            | "meta"
            | "nextid"
            | "param"
            | "source"
            | "spacer"
            | "track"
            | "wbr"
    )
}

impl Dom {
    pub fn new() -> Self {
        Self(vec![Node {
            kind: Kind::Tag(String::new(), Attrs::new()),
            parent: None,
            children: vec![],
        }])
    }

    pub fn parse(markup: &str) -> Self {
        let mut builder = Builder::new();
        if let Some(tokens) = super::tokenizer::scan(markup) {
            for (kind, value, attrs) in tokens {
                match kind {
                    0 => builder.pending.push_str(value),
                    1 | 2 => builder.start(value, attrs.unwrap_or_default(), kind == 2),
                    3 => builder.end(value),
                    4 => builder.special(Kind::Raw(format!("<!--{value}-->"))),
                    5 => builder.special(Kind::Raw(format!("<!DOCTYPE {value}>\n"))),
                    6 => builder.special(Kind::Pi(value.to_owned())),
                    7 => builder.cdata(value),
                    _ => unreachable!(),
                }
            }
        } else {
            let tokenizer = Tokenizer::new(
                Sink {
                    builder: RefCell::new(builder),
                    maybe_cdata: Cell::new(false),
                    processing_instruction: Cell::new(false),
                },
                Default::default(),
            );
            let input = BufferQueue::default();
            input.push_back(StrTendril::from(markup));
            let _ = tokenizer.feed(&input);
            tokenizer.end();
            builder = tokenizer.sink.builder.into_inner();
        }
        builder.flush();
        builder.dom
    }

    pub fn from_xml(root: roxmltree::Node<'_, '_>) -> Self {
        let mut dom = Self::new();
        let input = root.document().input_text();
        let mut stack = vec![(0, root)];
        while let Some((parent, node)) = stack.pop() {
            let kind = match node.node_type() {
                roxmltree::NodeType::Element => {
                    // Keep the original QNames. Looking up a prefix by URI can
                    // select the default binding even for a prefixed attribute.
                    // roxmltree exposes in-scope namespaces, so read the start
                    // tag to also retain local declarations equal to the inherited binding.
                    let mut reader = quick_xml::Reader::from_str(&input[node.range()]);
                    let start = match reader.read_event() {
                        Ok(
                            quick_xml::events::Event::Start(start)
                            | quick_xml::events::Event::Empty(start),
                        ) => start,
                        _ => unreachable!("roxmltree element has no XML start tag"),
                    };
                    let qualified = std::str::from_utf8(start.name().as_ref())
                        .unwrap()
                        .to_owned();
                    let mut attrs = Attrs::new();
                    if parent == 0 {
                        // The caller may have selected a subtree rather than the document root.
                        for ns in node.namespaces() {
                            let key = match ns.name() {
                                Some("xml") => continue,
                                Some(prefix) => format!("xmlns:{prefix}"),
                                None => "xmlns".to_owned(),
                            };
                            attrs.insert(key, ns.uri().to_owned());
                        }
                    } else {
                        for attr in start.attributes() {
                            let attr = attr.expect("roxmltree accepted an invalid attribute");
                            let name = std::str::from_utf8(attr.key.as_ref()).unwrap();
                            let prefix = if name == "xmlns" {
                                None
                            } else {
                                match name.strip_prefix("xmlns:") {
                                    Some("xml") | None => continue,
                                    prefix => prefix,
                                }
                            };
                            let uri = node.lookup_namespace_uri(prefix).unwrap();
                            attrs.insert(name.to_owned(), uri.to_owned());
                        }
                    }
                    for attr in node.attributes() {
                        let name = input[attr.range()]
                            .split([' ', '\t', '\r', '\n', '='])
                            .next()
                            .unwrap();
                        attrs.insert(name.to_owned(), attr.value().to_owned());
                    }
                    Kind::Tag(qualified, attrs)
                }
                roxmltree::NodeType::Text => Kind::Text(node.text().unwrap_or_default().to_owned()),
                roxmltree::NodeType::Comment => {
                    Kind::Raw(format!("<!--{}-->", node.text().unwrap_or_default()))
                }
                roxmltree::NodeType::PI => {
                    let pi = node.pi().unwrap();
                    Kind::Pi(pi.value.map_or_else(
                        || format!("{}?", pi.target),
                        |value| format!("{} {value}?", pi.target),
                    ))
                }
                _ => continue,
            };
            let id = dom.node(kind);
            dom.append(parent, id);
            stack.extend(node.children().rev().map(|node| (id, node)));
        }
        dom
    }

    pub fn len(&self) -> usize {
        self.0.len()
    }

    pub fn node(&mut self, kind: Kind) -> Id {
        let id = self.0.len();
        self.0.push(Node {
            kind,
            parent: None,
            children: vec![],
        });
        id
    }

    pub fn tag(&mut self, name: &str) -> Id {
        self.node(Kind::Tag(name.to_owned(), Attrs::new()))
    }

    pub fn kind(&self, id: Id) -> &Kind {
        &self.0[id].kind
    }

    pub fn name(&self, id: Id) -> &str {
        match self.kind(id) {
            Kind::Tag(name, _) => name,
            _ => "",
        }
    }

    pub fn attr(&self, id: Id, key: &str) -> Option<&str> {
        match self.kind(id) {
            Kind::Tag(_, attrs) => attrs.get(key).map(String::as_str),
            _ => None,
        }
    }

    pub fn attrs_mut(&mut self, id: Id) -> &mut Attrs {
        let Kind::Tag(_, attrs) = &mut self.0[id].kind else {
            unreachable!()
        };
        attrs
    }

    pub fn set(&mut self, id: Id, key: &str, value: impl Into<String>) {
        self.attrs_mut(id).insert(key.to_owned(), value.into());
    }

    pub fn children(&self, id: Id) -> &[Id] {
        &self.0[id].children
    }
    pub fn parent(&self, id: Id) -> Option<Id> {
        self.0[id].parent
    }
    pub fn parents(&self, id: Id) -> impl Iterator<Item = Id> + '_ {
        std::iter::successors(self.parent(id), |&parent| self.parent(parent))
            .filter(|&node| node != 0)
    }

    pub fn walk(&self, root: Id) -> Walk<'_> {
        Walk {
            dom: self,
            stack: vec![self.children(root).iter()],
        }
    }

    pub fn tags(&self, root: Id) -> impl Iterator<Item = Id> + '_ {
        self.walk(root).filter(|&id| !self.name(id).is_empty())
    }

    pub fn find(&self, root: Id, name: &str) -> Option<Id> {
        self.tags(root).find(|&id| self.name(id) == name)
    }

    pub fn direct(&self, root: Id, name: &str) -> Option<Id> {
        self.children(root)
            .iter()
            .copied()
            .find(|&id| self.name(id) == name)
    }

    pub fn detach(&mut self, id: Id) {
        if let Some(parent) = self.0[id].parent.take() {
            self.0[parent].children.retain(|&child| child != id);
        }
    }

    pub fn append(&mut self, parent: Id, id: Id) {
        self.detach(id);
        self.0[id].parent = Some(parent);
        self.0[parent].children.push(id);
    }

    pub fn insert(&mut self, sibling: Id, id: Id, after: bool) {
        self.detach(id);
        if let Some(parent) = self.parent(sibling) {
            let pos = self
                .children(parent)
                .iter()
                .position(|&n| n == sibling)
                .unwrap();
            self.0[id].parent = Some(parent);
            self.0[parent].children.insert(pos + usize::from(after), id);
        }
    }

    pub fn prepend(&mut self, parent: Id, id: Id) {
        if let Some(&first) = self.children(parent).first() {
            self.insert(first, id, false);
        } else {
            self.append(parent, id);
        }
    }

    pub fn clear(&mut self, id: Id) {
        for child in std::mem::take(&mut self.0[id].children) {
            self.0[child].parent = None;
        }
    }

    pub fn take_children(&mut self, id: Id) -> Vec<Id> {
        let children = std::mem::take(&mut self.0[id].children);
        for &child in &children {
            self.0[child].parent = None;
        }
        children
    }

    pub fn set_children(&mut self, parent: Id, children: Vec<Id>) {
        for &child in &children {
            self.0[child].parent = Some(parent);
        }
        self.0[parent].children = children;
    }

    pub fn wrap_detached(&mut self, name: &str, attr: (&str, &str), children: Vec<Id>) -> Id {
        let span = self.tag(name);
        self.set(span, attr.0, attr.1);
        for &child in &children {
            self.0[child].parent = Some(span);
        }
        self.0[span].children = children;
        span
    }

    pub fn text_mut(&mut self, id: Id) -> Option<&mut String> {
        match &mut self.0[id].kind {
            Kind::Text(text) => Some(text),
            _ => None,
        }
    }

    pub fn unwrap(&mut self, id: Id) {
        for child in self.children(id).to_vec() {
            self.insert(id, child, false);
        }
        self.detach(id);
    }

    pub fn append_text(&mut self, parent: Id, text: &str) {
        if !text.is_empty() {
            let id = self.node(Kind::Text(text.to_owned()));
            self.append(parent, id);
        }
    }

    pub fn copy(&mut self, id: Id) -> Id {
        let root = self.node(self.kind(id).clone());
        let mut stack = vec![(id, root)];
        while let Some((from, to)) = stack.pop() {
            for child in self.children(from).to_vec() {
                let clone = self.node(self.kind(child).clone());
                self.append(to, clone);
                stack.push((child, clone));
            }
        }
        root
    }

    pub fn text(&self, root: Id, sep: &str, trim: bool) -> String {
        let mut result = String::new();
        for id in self.walk(root) {
            if let Kind::Text(value) = self.kind(id) {
                let value = if trim { strip(value) } else { value };
                if !value.is_empty() {
                    if !result.is_empty() {
                        result.push_str(sep);
                    }
                    result.push_str(value);
                }
            }
        }
        result
    }

    pub fn meaningful(&self, root: Id) -> bool {
        self.walk(root)
            .any(|id| matches!(self.kind(id), Kind::Text(text) if !strip(text).is_empty()))
    }

    pub fn html(&self, id: Id) -> String {
        self.serialize(id, false)
    }

    pub(super) fn epub_html(mut self) -> String {
        let metas: Vec<_> = self.tags(0).filter(|&id| self.name(id) == "meta").collect();
        for id in metas {
            let attrs = self.attrs_mut(id);
            if let Some(label) = attrs.get_mut("charset") {
                if !label.trim_ascii().eq_ignore_ascii_case("utf-8") {
                    *label = "utf-8".to_owned();
                }
            }
            let range = attrs.get("content").and_then(|content| {
                let (range, label) =
                    pragma_charset(attrs.get("http-equiv").map(String::as_str), content)?;
                (!label.eq_ignore_ascii_case("utf-8")).then_some(range)
            });
            if let Some(range) = range {
                attrs
                    .get_mut("content")
                    .unwrap()
                    .replace_range(range, "utf-8");
            }
        }
        self.html(0)
    }

    pub fn inner_html(&self, id: Id) -> String {
        self.serialize_nodes(self.children(id).iter().copied(), false)
    }

    pub fn xml(&self, id: Id) -> String {
        self.serialize(id, true)
    }

    fn serialize(&self, id: Id, xml: bool) -> String {
        self.serialize_nodes(std::iter::once(id), xml)
    }

    fn serialize_nodes(&self, roots: impl DoubleEndedIterator<Item = Id>, xml: bool) -> String {
        let mut out = String::new();
        let mut stack: Vec<_> = roots.rev().map(|id| (id, false)).collect();
        while let Some((id, closing)) = stack.pop() {
            match self.kind(id) {
                Kind::Text(text) => {
                    if !xml
                        && self
                            .parent(id)
                            .is_some_and(|p| matches!(self.name(p), "script" | "style"))
                    {
                        out.push_str(text);
                    } else {
                        escape(&mut out, text, false);
                    }
                }
                Kind::Raw(text) => out.push_str(text),
                Kind::Pi(text) => {
                    out.push_str("<?");
                    out.push_str(text);
                    out.push('>');
                }
                Kind::Tag(name, attrs) => {
                    if closing {
                        out.push_str("</");
                        out.push_str(name);
                        out.push('>');
                        continue;
                    }
                    if !name.is_empty() {
                        out.push('<');
                        out.push_str(name);
                        for (key, value) in attrs {
                            out.push(' ');
                            out.push_str(key);
                            out.push('=');
                            if !xml && value.contains('"') && !value.contains('\'') {
                                out.push('\'');
                                escape(&mut out, value, false);
                                out.push('\'');
                            } else {
                                out.push('"');
                                escape(&mut out, value, true);
                                out.push('"');
                            }
                        }
                        if self.children(id).is_empty() && (xml || void(name)) {
                            out.push_str("/>");
                            continue;
                        }
                        out.push('>');
                        stack.push((id, true));
                    }
                    stack.extend(self.children(id).iter().rev().map(|&child| (child, false)));
                }
            }
        }
        out
    }
}

pub struct Walk<'a> {
    dom: &'a Dom,
    stack: Vec<std::slice::Iter<'a, Id>>,
}

impl Iterator for Walk<'_> {
    type Item = Id;
    fn next(&mut self) -> Option<Id> {
        while let Some(iter) = self.stack.last_mut() {
            if let Some(&node) = iter.next() {
                if !self.dom.children(node).is_empty() {
                    self.stack.push(self.dom.children(node).iter());
                }
                return Some(node);
            }
            self.stack.pop();
        }
        None
    }
}

struct Builder {
    dom: Dom,
    open: Vec<Id>,
    pending: String,
    preserve_spaces: usize,
}

impl Builder {
    fn new() -> Self {
        Self {
            dom: Dom::new(),
            open: vec![0],
            pending: String::new(),
            preserve_spaces: 0,
        }
    }

    fn flush(&mut self) {
        if self.pending.is_empty() {
            return;
        }
        let mut text = std::mem::take(&mut self.pending);
        if self.preserve_spaces == 0
            && text
                .bytes()
                .all(|c| matches!(c, b' ' | b'\t' | b'\r' | b'\n' | b'\x0c'))
        {
            text = if text.contains('\n') { "\n" } else { " " }.to_owned();
        }
        let node = self.dom.node(Kind::Text(text));
        self.dom.append(*self.open.last().unwrap(), node);
    }

    fn cdata(&mut self, value: &str) {
        // Legacy HTMLParser makes CDATA a separate text node; handle whitespace
        // independently and do not resolve entity references inside it.
        self.flush();
        let text = if value.is_empty() && self.preserve_spaces == 0 {
            " "
        } else {
            value
        };
        self.pending.push_str(text);
        self.flush();
    }

    fn start(
        &mut self,
        name: &str,
        attrs: impl IntoIterator<Item = (impl Into<String>, String)>,
        empty: bool,
    ) {
        self.flush();
        let attributes = attrs
            .into_iter()
            .map(|(key, value)| {
                let key = key.into();
                let value = if matches!(key.as_str(), "class" | "accesskey" | "dropzone")
                    || matches!(
                        (name, key.as_str()),
                        ("a" | "link" | "area", "rel" | "rev")
                            | ("td" | "th", "headers")
                            | ("form", "accept-charset")
                            | ("iframe", "sandbox")
                            | ("output", "for")
                            | ("object", "archive")
                    ) {
                    value.split_whitespace().collect::<Vec<_>>().join(" ")
                } else {
                    value
                };
                (key, value)
            })
            .collect();
        let node = self.dom.node(Kind::Tag(name.to_string(), attributes));
        self.dom.append(*self.open.last().unwrap(), node);
        if !empty && !void(name) {
            self.open.push(node);
            self.preserve_spaces += usize::from(matches!(name, "pre" | "textarea"));
        }
    }

    fn end(&mut self, name: &str) {
        self.flush();
        if let Some(pos) = self
            .open
            .iter()
            .rposition(|&node| self.dom.name(node) == name)
        {
            for id in self.open.drain(pos..) {
                self.preserve_spaces -=
                    usize::from(matches!(self.dom.name(id), "pre" | "textarea"));
            }
        }
    }

    fn special(&mut self, kind: Kind) {
        self.flush();
        let node = self.dom.node(kind);
        self.dom.append(*self.open.last().unwrap(), node);
    }
}

struct Sink {
    builder: RefCell<Builder>,
    maybe_cdata: Cell<bool>,
    processing_instruction: Cell<bool>,
}

impl TokenSink for Sink {
    type Handle = ();

    fn adjusted_current_node_present_but_not_in_html_namespace(&self) -> bool {
        // In EPUB XHTML, CDATA is valid even in the HTML namespace. The tokenizer
        // calls this before distinguishing CDATA from other (bogus) declarations:
        // the next content token is either its literal text or a comment.
        self.maybe_cdata.set(true);
        true
    }

    fn process_token(&self, token: Token, _: u64) -> TokenSinkResult<()> {
        let mut b = self.builder.borrow_mut();
        match token {
            Token::CharacterTokens(text) if self.maybe_cdata.replace(false) => b.cdata(&text),
            Token::CharacterTokens(text) => b.pending.push_str(&text),
            Token::NullCharacterToken => b.pending.push('\0'),
            Token::TagToken(tag) => {
                let name = tag.name.as_ref();
                if tag.kind == html5ever::tokenizer::StartTag {
                    b.start(
                        name,
                        tag.attrs
                            .into_iter()
                            .map(|a| (a.name.local.to_string(), a.value.to_string())),
                        tag.self_closing,
                    );
                    if !tag.self_closing && matches!(name, "script" | "style") {
                        return TokenSinkResult::RawData(if name == "script" {
                            RawKind::ScriptData
                        } else {
                            RawKind::Rawtext
                        });
                    }
                } else {
                    b.end(name);
                }
            }
            Token::ParseError(msg) if msg == "Saw ? in state TagOpen" => {
                // html5ever delivers a PI as a bogus comment after this
                // diagnostic. Real comments can have the same token text.
                self.processing_instruction.set(true);
            }
            Token::CommentToken(text) => {
                self.maybe_cdata.set(false);
                if self.processing_instruction.replace(false)
                    && text.starts_with('?')
                    && text.ends_with('?')
                {
                    b.special(Kind::Pi(text[1..].to_owned()));
                } else {
                    b.special(Kind::Raw(format!("<!--{text}-->")));
                }
            }
            Token::DoctypeToken(doc) => {
                let mut decl = format!("<!DOCTYPE {}", doc.name.as_deref().unwrap_or("html"));
                if let Some(id) = doc.public_id {
                    decl.push_str(&format!(" PUBLIC \"{id}\""));
                } else if doc.system_id.is_some() {
                    decl.push_str(" SYSTEM");
                }
                if let Some(id) = doc.system_id {
                    decl.push_str(&format!(" \"{id}\""));
                }
                decl.push_str(">\n");
                b.special(Kind::Raw(decl));
            }
            _ => {}
        }
        TokenSinkResult::Continue
    }
}

pub fn decode(data: &[u8]) -> Cow<'_, str> {
    decode_markup(data, true)
}

pub(super) fn decode_xml(data: &[u8]) -> Cow<'_, str> {
    decode_markup(data, false)
}

fn utf16_signature(data: &[u8]) -> Option<&'static Encoding> {
    // XML starts with '<' (a declaration, comment, or root tag), possibly
    // preceded by XML whitespace. In UTF-16 its ASCII code units expose endian
    // order even without a BOM or an encoding declaration.
    match data {
        [b'<' | b' ' | b'\t' | b'\r' | b'\n', 0, 1..=127, 0, ..] => Some(UTF_16LE),
        [0, b'<' | b' ' | b'\t' | b'\r' | b'\n', 0, 1..=127, ..] => Some(UTF_16BE),
        _ => None,
    }
}

fn decode_markup(data: &[u8], html: bool) -> Cow<'_, str> {
    if let Some((encoding, bom_size)) = Encoding::for_bom(data) {
        return encoding.decode_without_bom_handling(&data[bom_size..]).0;
    }
    if let Some(encoding) = utf16_signature(data) {
        return encoding.decode_without_bom_handling(data).0;
    }
    let declared = xml_encoding(data).and_then(|label| Encoding::for_label(&data[label]));
    let decode = |encoding: &'static Encoding| {
        encoding.decode_without_bom_handling_and_without_replacement(data)
    };
    if let Some(text) = declared
        .and_then(decode)
        .or_else(|| html.then(|| html_encoding(data)).flatten().and_then(decode))
    {
        return text;
    }
    if let Ok(text) = std::str::from_utf8(data) {
        return Cow::Borrowed(text);
    }

    // Recover unlabeled legacy text from the original bytes, as UnicodeDammit
    // did. Do not guess based on the translation language or decode an already
    // decoded string. BOMs, usable declarations and UTF-8 never use the detector.
    let mut detector = EncodingDetector::new(Iso2022JpDetection::Deny);
    detector.feed(data, true);
    detector
        .guess(None, Utf8Detection::Deny)
        .decode_without_bom_handling_and_without_replacement(data)
        .unwrap_or_else(|| WINDOWS_1252.decode_without_bom_handling(data).0)
}
