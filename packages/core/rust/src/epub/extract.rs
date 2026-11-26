//! Stable segment identities, inline objects and note metadata.

use std::{
    collections::{HashMap, HashSet},
    sync::LazyLock,
};

use regex::Regex;
use serde::Serialize;
use serde_json::{json, Value};

use super::{
    dom::{strip, Dom, Id, Kind},
    links::resolve,
};

pub const ANCHOR: &str = "data-tn-id";
pub const ANNOTATION: &str = "data-tn-annotation-id";
pub const INLINE: &str = "data-tn-inline-id";
pub const LINE: &str = "data-tn-line";
pub const OBJECT: &str = "inline_object";
pub const EPUB_SCHEMA: u32 = 7;

#[derive(Serialize)]
pub struct Segment {
    pub index: usize,
    pub source: String,
    pub kind: &'static str,
    pub anchor: String,
    pub resource_href: String,
    pub meta: Value,
    #[serde(skip)]
    pub node: Id,
}

pub struct Resource {
    pub dom: Dom,
    pub title: String,
    pub segments: Vec<Segment>,
}

fn block(name: &str) -> bool {
    matches!(
        name,
        "p" | "li" | "blockquote" | "td" | "th" | "dt" | "dd" | "figcaption"
    ) || heading(name)
}

fn candidate(name: &str) -> bool {
    name == "div" || block(name)
}
fn loose_text_container(name: &str) -> bool {
    matches!(
        name,
        "body"
            | "div"
            | "section"
            | "article"
            | "main"
            | "aside"
            | "blockquote"
            | "dd"
            | "header"
            | "footer"
            | "figure"
            | "details"
            | "summary"
            | "address"
    )
}
fn non_prose(name: &str) -> bool {
    matches!(
        name,
        "head" | "script" | "style" | "noscript" | "template" | "title" | "meta" | "link"
    )
}
fn structural_boundary(name: &str) -> bool {
    // Note: `hr` is also in `atomic` so `<p>` treats it as an inline object, while
    // loose-text containers treat it as a block thematic break between prose runs.
    matches!(
        name,
        "section"
            | "article"
            | "main"
            | "aside"
            | "header"
            | "footer"
            | "nav"
            | "ul"
            | "ol"
            | "dl"
            | "table"
            | "thead"
            | "tbody"
            | "tfoot"
            | "tr"
            | "figure"
            | "hr"
            | "pre"
            | "form"
            | "details"
            | "summary"
            | "address"
            | "fieldset"
    )
}
fn heading(name: &str) -> bool {
    matches!(name, "h1" | "h2" | "h3" | "h4" | "h5" | "h6")
}
fn atomic(name: &str) -> bool {
    matches!(
        name,
        "audio"
            | "canvas"
            | "embed"
            | "hr"
            | "iframe"
            | "img"
            | "math"
            | "object"
            | "source"
            | "svg"
            | "video"
    )
}

const F_TEXT: u8 = 1 << 0;
const F_PROSE: u8 = 1 << 1;
const F_DESC_CANDIDATE: u8 = 1 << 2;
const F_DESC_ANY_BLOCK: u8 = 1 << 3;
const F_DESC_SPLIT: u8 = 1 << 4;
const F_DESC_NON_PROSE: u8 = 1 << 5;

fn is_ascii_ws(c: char) -> bool {
    matches!(c, ' ' | '\t' | '\n' | '\r' | '\x0b' | '\x0c')
}

fn is_internal_href(dom: &Dom, id: Id) -> bool {
    dom.name(id) == "a"
        && dom
            .attr(id, "href")
            .is_some_and(|raw| !strip(raw).is_empty() && !resolve("", raw).external)
}

fn trailing_note_marker(dom: &Dom, link: Id) -> Option<Id> {
    dom.children(link)
        .iter()
        .rev()
        .copied()
        .find(|&id| !matches!(dom.kind(id), Kind::Text(s) if strip(s).is_empty()))
        .filter(|&id| {
            if !matches!(dom.name(id), "sup" | "sub") {
                return false;
            }
            let text = dom.text(id, "", true);
            MARKER.is_match(&text)
                && (dom.name(id) != "sub" || DECORATED.is_match(&text))
                && hint(dom, link, id, &text, true)
        })
}

fn is_note_link(dom: &Dom, link: Id) -> bool {
    is_internal_href(dom, link) && trailing_note_marker(dom, link).is_some()
}

fn compute_flags(dom: &Dom) -> Vec<u8> {
    let mut flags = vec![0u8; dom.len()];
    let mut stack = vec![(0usize, false)];
    while let Some((id, visited)) = stack.pop() {
        if !visited {
            match dom.kind(id) {
                Kind::Text(text) => {
                    if !strip(text).is_empty() {
                        flags[id] = F_TEXT | F_PROSE;
                    }
                }
                Kind::Tag(..) => {
                    stack.push((id, true));
                    for &child in dom.children(id).iter().rev() {
                        stack.push((child, false));
                    }
                }
                Kind::Raw(_) | Kind::Pi(_) => {}
            }
            continue;
        }
        let name = dom.name(id);
        let prose_ok = !(atomic(name) || non_prose(name) || matches!(name, "rt" | "rp"));
        let mut f = 0u8;
        for &child in dom.children(id) {
            let cf = flags[child];
            if cf & F_TEXT != 0 {
                f |= F_TEXT;
            }
            if prose_ok && cf & F_PROSE != 0 {
                f |= F_PROSE;
            }
            if let Kind::Tag(cname, _) = dom.kind(child) {
                let cname = cname.as_str();
                let is_internal_link = is_internal_href(dom, child);
                if !atomic(cname)
                    && !non_prose(cname)
                    && ((candidate(cname) && cf & F_TEXT != 0) || cf & F_DESC_CANDIDATE != 0)
                {
                    f |= F_DESC_CANDIDATE;
                }
                if candidate(cname)
                    || structural_boundary(cname)
                    || non_prose(cname)
                    || (!atomic(cname) && cf & F_DESC_ANY_BLOCK != 0)
                {
                    f |= F_DESC_ANY_BLOCK;
                }
                if candidate(cname)
                    || structural_boundary(cname)
                    || non_prose(cname)
                    || cname == "br"
                    || (!atomic(cname) && !is_internal_link && cf & F_DESC_SPLIT != 0)
                    || (is_internal_link && cf & F_DESC_ANY_BLOCK != 0)
                {
                    f |= F_DESC_SPLIT;
                }
                if non_prose(cname) || (!atomic(cname) && cf & F_DESC_NON_PROSE != 0) {
                    f |= F_DESC_NON_PROSE;
                }
            }
        }
        flags[id] = f;
    }
    flags
}

fn push_break_lines(dom: &mut Dom, node: Id, out: &mut Vec<Id>) {
    if !dom.children(node).iter().any(|&n| dom.name(n) == "br") {
        out.push(node);
        return;
    }
    let runs: Vec<Vec<Id>> = dom
        .children(node)
        .split(|&n| dom.name(n) == "br")
        .filter(|run| run.iter().any(|&id| visible(dom, id)))
        .map(|run| {
            run.iter()
                .copied()
                .filter(|&id| matches!(dom.kind(id), Kind::Tag(..) | Kind::Text(_)))
                .collect()
        })
        .collect();
    for run in runs {
        if let Some(&first) = run.first() {
            let span = dom.tag("span");
            dom.set(span, LINE, "true");
            dom.insert(first, span, false);
            for id in run {
                dom.append(span, id);
            }
            out.push(span);
        }
    }
}

fn legacy_targets(dom: &mut Dom, skip_nav: bool) -> Vec<Id> {
    let choices: Vec<_> = dom.tags(0).filter(|&id| candidate(dom.name(id))).collect();
    let mut targets = Vec::new();
    for el in choices {
        if skip_nav {
            let mut li = dom.name(el) == "li";
            let mut excluded = false;
            for parent in dom.parents(el) {
                if dom.name(parent) == "nav" {
                    excluded = li;
                    break;
                }
                li |= dom.name(parent) == "li";
            }
            if excluded {
                continue;
            }
        }
        let descendant_block = dom
            .tags(el)
            .any(|id| candidate(dom.name(id)) && dom.meaningful(id));
        let link = (dom.name(el) == "li")
            .then(|| dom.direct(el, "a"))
            .flatten()
            .filter(|&link| {
                dom.meaningful(link)
                    && dom.children(el).iter().all(|&child| {
                        child == link
                            || candidate(dom.name(child))
                            || matches!(dom.name(child), "ul" | "ol" | "dl")
                            || !visible(dom, child)
                    })
            });
        if descendant_block && link.is_none() {
            continue;
        }
        let node = link.unwrap_or(el);
        push_break_lines(dom, node, &mut targets);
    }
    targets
}

enum Work {
    EmitTarget(Id),
    Visit {
        id: Id,
        in_nav: bool,
        in_nav_li: bool,
        loose_inline: bool,
    },
}

fn trim_inline_boundary_space(dom: &mut Dom, id: Id, leading: bool) -> Option<String> {
    let text = dom.text_mut(id)?;
    let trimmed = if leading {
        text.trim_start_matches(is_ascii_ws)
    } else {
        text.trim_end_matches(is_ascii_ws)
    };
    let ws_len = text.len() - trimmed.len();
    if ws_len == 0 {
        return None;
    }
    if leading {
        let ws = text[..ws_len].to_owned();
        text.drain(..ws_len);
        Some(ws)
    } else {
        let split_at = text.len() - ws_len;
        let ws = text[split_at..].to_owned();
        text.truncate(split_at);
        Some(ws)
    }
}

fn schema7_targets(dom: &mut Dom, skip_nav: bool) -> Vec<Id> {
    let flags = compute_flags(dom);
    let mut out = Vec::new();
    let mut stack = vec![Work::Visit {
        id: 0,
        in_nav: false,
        in_nav_li: false,
        loose_inline: false,
    }];
    while let Some(work) = stack.pop() {
        match work {
            Work::EmitTarget(span) => out.push(span),
            Work::Visit {
                id,
                in_nav,
                in_nav_li,
                loose_inline,
            } => {
                let name = dom.name(id).to_owned();
                if !name.is_empty() && (non_prose(&name) || atomic(&name)) {
                    continue;
                }
                let (next_nav, next_nav_li) = if name == "nav" {
                    (true, false)
                } else if in_nav && name == "li" {
                    (true, true)
                } else {
                    (in_nav, in_nav_li)
                };
                let excluded = skip_nav && next_nav_li;
                let node_flags = flags[id];

                if !name.is_empty() && candidate(&name) {
                    let link = (name == "li")
                        .then(|| dom.direct(id, "a"))
                        .flatten()
                        .filter(|&link| {
                            flags[link] & F_TEXT != 0
                                && dom.children(id).iter().all(|&child| {
                                    child == link
                                        || candidate(dom.name(child))
                                        || matches!(dom.name(child), "ul" | "ol" | "dl")
                                        || flags[child] & F_TEXT == 0
                                })
                        });
                    if let Some(link_id) = link {
                        let split_link = (flags[link_id] & F_DESC_ANY_BLOCK != 0)
                            || (flags[link_id] & F_DESC_SPLIT != 0 && !is_note_link(dom, link_id));
                        if !split_link && !excluded {
                            out.push(link_id);
                        }
                        for &child in dom.children(id).iter().rev() {
                            if child == link_id {
                                if split_link {
                                    stack.push(Work::Visit {
                                        id: link_id,
                                        in_nav: next_nav,
                                        in_nav_li: next_nav_li,
                                        loose_inline: !excluded,
                                    });
                                }
                            } else if matches!(dom.kind(child), Kind::Tag(..)) {
                                stack.push(Work::Visit {
                                    id: child,
                                    in_nav: next_nav,
                                    in_nav_li: next_nav_li,
                                    loose_inline: false,
                                });
                            }
                        }
                        continue;
                    }
                    let descend = (node_flags & F_DESC_CANDIDATE != 0)
                        || (node_flags & F_DESC_NON_PROSE != 0)
                        || (loose_text_container(&name) && node_flags & F_DESC_SPLIT != 0);
                    if !descend {
                        if !excluded {
                            push_break_lines(dom, id, &mut out);
                        }
                        continue;
                    }
                }

                let loose_mode = !excluded
                    && (loose_text_container(&name)
                        || loose_inline
                        || (candidate(&name) && node_flags & F_DESC_NON_PROSE != 0));
                if loose_mode {
                    let old_children = dom.take_children(id);
                    let mut new_children = Vec::with_capacity(old_children.len());
                    let mut items = Vec::new();
                    let mut run = Vec::new();
                    let mut prev_inline_split = loose_inline;

                    let flush = |dom: &mut Dom,
                                 run: &mut Vec<Id>,
                                 new_children: &mut Vec<Id>,
                                 items: &mut Vec<Work>,
                                 lead_split: bool,
                                 trail_split: bool| {
                        if run.is_empty() {
                            return;
                        }
                        if run.iter().any(|&n| flags[n] & F_PROSE != 0) {
                            let mut content = Vec::with_capacity(run.len());
                            let mut trivia = Vec::new();
                            for n in run.drain(..) {
                                if matches!(dom.kind(n), Kind::Raw(_) | Kind::Pi(_)) {
                                    trivia.push(n);
                                } else {
                                    content.push(n);
                                }
                            }
                            let lead_ws = if lead_split {
                                trim_inline_boundary_space(dom, content[0], true)
                            } else {
                                None
                            };
                            if lead_ws.is_some()
                                && content.len() > 1
                                && matches!(dom.kind(content[0]), Kind::Text(s) if s.is_empty())
                            {
                                content.remove(0);
                            }
                            let trail_ws = if trail_split {
                                trim_inline_boundary_space(dom, *content.last().unwrap(), false)
                            } else {
                                None
                            };
                            if trail_ws.is_some()
                                && content.len() > 1
                                && matches!(
                                    dom.kind(*content.last().unwrap()),
                                    Kind::Text(s) if s.is_empty()
                                )
                            {
                                content.pop();
                            }
                            if let Some(ws) = lead_ws {
                                new_children.push(dom.node(Kind::Text(ws)));
                            }
                            let span = dom.wrap_detached("span", (LINE, "true"), content);
                            new_children.push(span);
                            items.push(Work::EmitTarget(span));
                            if let Some(ws) = trail_ws {
                                new_children.push(dom.node(Kind::Text(ws)));
                            }
                            new_children.extend(trivia);
                        } else {
                            new_children.extend(run.drain(..));
                        }
                    };

                    for child in old_children {
                        match dom.kind(child) {
                            Kind::Tag(tag, _) => {
                                let tag = tag.as_str();
                                if tag == "br" {
                                    flush(
                                        dom,
                                        &mut run,
                                        &mut new_children,
                                        &mut items,
                                        prev_inline_split,
                                        false,
                                    );
                                    new_children.push(child);
                                    prev_inline_split = false;
                                } else if non_prose(tag) {
                                    flush(
                                        dom,
                                        &mut run,
                                        &mut new_children,
                                        &mut items,
                                        prev_inline_split,
                                        true,
                                    );
                                    new_children.push(child);
                                    prev_inline_split = true;
                                } else if candidate(tag) || structural_boundary(tag) {
                                    flush(
                                        dom,
                                        &mut run,
                                        &mut new_children,
                                        &mut items,
                                        prev_inline_split,
                                        false,
                                    );
                                    new_children.push(child);
                                    items.push(Work::Visit {
                                        id: child,
                                        in_nav: next_nav,
                                        in_nav_li: next_nav_li,
                                        loose_inline: false,
                                    });
                                    prev_inline_split = false;
                                } else if !atomic(tag)
                                    && !(is_internal_href(dom, child)
                                        && flags[child] & F_DESC_ANY_BLOCK == 0)
                                    && flags[child] & F_DESC_SPLIT != 0
                                {
                                    flush(
                                        dom,
                                        &mut run,
                                        &mut new_children,
                                        &mut items,
                                        prev_inline_split,
                                        true,
                                    );
                                    new_children.push(child);
                                    items.push(Work::Visit {
                                        id: child,
                                        in_nav: next_nav,
                                        in_nav_li: next_nav_li,
                                        loose_inline: true,
                                    });
                                    prev_inline_split = true;
                                } else {
                                    run.push(child);
                                }
                            }
                            Kind::Text(_) => run.push(child),
                            Kind::Raw(_) | Kind::Pi(_) => {
                                // Keep comments and processing instructions outside runs so backfill
                                // never deletes them, without splitting a sentence mid-run or
                                // reordering them ahead of an active run.
                                if run.is_empty() {
                                    new_children.push(child);
                                } else {
                                    run.push(child);
                                }
                            }
                        }
                    }
                    flush(
                        dom,
                        &mut run,
                        &mut new_children,
                        &mut items,
                        prev_inline_split,
                        loose_inline,
                    );
                    dom.set_children(id, new_children);
                    for item in items.into_iter().rev() {
                        stack.push(item);
                    }
                    continue;
                }

                for &child in dom.children(id).iter().rev() {
                    if matches!(dom.kind(child), Kind::Tag(..)) {
                        stack.push(Work::Visit {
                            id: child,
                            in_nav: next_nav,
                            in_nav_li: next_nav_li,
                            loose_inline: false,
                        });
                    }
                }
            }
        }
    }
    out
}

fn targets(dom: &mut Dom, skip_nav: bool, loose_text: bool) -> Vec<Id> {
    if loose_text {
        schema7_targets(dom, skip_nav)
    } else {
        legacy_targets(dom, skip_nav)
    }
}

fn visible(dom: &Dom, id: Id) -> bool {
    match dom.kind(id) {
        Kind::Text(text) => !strip(text).is_empty(),
        Kind::Tag(..) => dom.meaningful(id),
        _ => false,
    }
}

static MARKER: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[\d\s*＊※†‡\[\]()〔〕（）{}↩↵←↑↓⤶.·:：\-]+$").unwrap());
static DECORATED: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[^\d\s.·:\-]").unwrap());
static HINT: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)(?:^|[^a-z0-9])(?:note|noteref|footnote|endnote|fn|jpref|jpnote)(?:[-_]?\d+)?(?:$|[^a-z0-9])").unwrap()
});
static NUMBERED: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)(?:notes?|footnotes?|endnotes?|fn)[\-_]?\d+$").unwrap());
static NOTE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)(?:^|[^a-z0-9])(?:note|footnote|endnote|fn)(?:[-_]?\d+)?(?:$|[^a-z0-9])")
        .unwrap()
});
static SHORT: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?i)^n[-_]?\d+$").unwrap());
static VERTICAL_ALIGN: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)(?:^|;)\s*vertical-align\s*:\s*(?:super|sub)\b").unwrap());
static SPACE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[ \t\r\n\x0c\x0b]+").unwrap());

fn semantic(dom: &Dom, node: Id, words: &[&str]) -> bool {
    ["epub:type", "role", "rel"].iter().any(|attr| {
        dom.attr(node, attr).is_some_and(|s| {
            s.split_whitespace().any(|word| {
                let word = word.to_ascii_lowercase();
                words.contains(&word.rsplit(':').next().unwrap())
            })
        })
    })
}

fn scope(dom: &Dom, node: Id, predicate: impl Fn(Id) -> bool) -> Option<Id> {
    std::iter::once(node)
        .chain(dom.parents(node))
        .find(|&id| predicate(id))
}

fn semantic_scope(dom: &Dom, node: Id) -> Option<Id> {
    scope(dom, node, |id| {
        semantic(
            dom,
            id,
            &[
                "footnote",
                "endnote",
                "rearnote",
                "doc-footnote",
                "doc-endnote",
            ],
        )
    })
}

fn has_note_id(dom: &Dom, id: Id) -> bool {
    ["id", "class", "name"]
        .iter()
        .any(|a| dom.attr(id, a).is_some_and(|s| NOTE.is_match(s)))
}

fn implicit_scope(dom: &Dom, node: Id) -> Option<Id> {
    scope(dom, node, |id| {
        matches!(
            dom.name(id),
            "aside" | "li" | "dd" | "p" | "div" | "section"
        ) && has_note_id(dom, id)
    })
}

fn short_scope(dom: &Dom, node: Id) -> Option<Id> {
    scope(dom, node, |id| {
        matches!(dom.name(id), "aside" | "li" | "dd" | "p")
            && ["id", "name"]
                .iter()
                .any(|a| dom.attr(id, a).is_some_and(|s| SHORT.is_match(s)))
    })
}

fn surrounding(dom: &Dom, block: Id, mut node: Id) -> (bool, bool) {
    let (mut before, mut after) = (false, false);
    while node != block {
        let Some(parent) = dom.parent(node) else {
            break;
        };
        if parent == 0 {
            break;
        }
        let (left, right) = dom.children(parent).split_at(
            dom.children(parent)
                .iter()
                .position(|&id| id == node)
                .unwrap(),
        );
        before |= left.iter().any(|&id| visible(dom, id));
        after |= right[1..].iter().any(|&id| visible(dom, id));
        node = parent;
    }
    (before, after)
}

fn inside_implicit(dom: &Dom, link: Id, text: &str) -> bool {
    implicit_scope(dom, link).is_some()
        || (MARKER.is_match(text)
            && short_scope(dom, link).is_some_and(|id| {
                let (before, after) = surrounding(dom, id, link);
                before != after
            }))
}

fn hint(dom: &Dom, link: Id, marker: Id, text: &str, allow_short: bool) -> bool {
    let fragment = dom
        .attr(link, "href")
        .and_then(|s| s.split_once('#'))
        .map_or("", |(_, f)| f);
    DECORATED.is_match(text)
        || HINT.is_match(fragment)
        || NUMBERED.is_match(fragment)
        || (allow_short && SHORT.is_match(fragment))
        || [link, marker].into_iter().any(|id| {
            ["id", "class", "role", "rel", "epub:type"]
                .iter()
                .any(|a| dom.attr(id, a).is_some_and(|s| HINT.is_match(s)))
        })
}

fn is_super(dom: &Dom, id: Id) -> bool {
    matches!(dom.name(id), "sup" | "sub")
        || (dom.name(id) == "span"
            && (dom.attr(id, "class").is_some_and(|s| {
                s.split_whitespace().any(|s| {
                    matches!(
                        s.to_ascii_lowercase().as_str(),
                        "sup" | "sub" | "super" | "subscript" | "superscript"
                    )
                })
            }) || dom
                .attr(id, "style")
                .is_some_and(|s| VERTICAL_ALIGN.is_match(s))))
}

fn relation(
    dom: &Dom,
    link: Id,
    block: Id,
    point: bool,
    wrapped: bool,
    range: bool,
    text: &str,
) -> &'static str {
    if semantic(dom, link, &["backlink", "doc-backlink"]) {
        return "backlink";
    }
    if semantic(dom, link, &["noteref", "doc-noteref"]) {
        return "noteref";
    }
    if semantic_scope(dom, link).is_some()
        || point
            && (inside_implicit(dom, link, text)
                || text
                    .chars()
                    .all(|c| c.is_whitespace() || "↩↵←↑↓⤶".contains(c))
                || (!wrapped && link != block && surrounding(dom, block, link) == (false, true)))
    {
        return "backlink";
    }
    if wrapped || range || point {
        "noteref"
    } else {
        "internal_link"
    }
}

struct Annotation {
    node: Id,
    id: String,
    marker: Option<Id>,
    point: bool,
    marker_text: String,
    href: String,
    target_key: String,
    relation: &'static str,
}

fn annotations(dom: &mut Dom, block: Id, anchor: &str, href: &str) -> Vec<Annotation> {
    let mut links = Vec::new();
    if dom.name(block) == "a"
        && dom
            .tags(block)
            .any(|id| matches!(dom.name(id), "sup" | "sub"))
    {
        links.push(block);
    }
    links.extend(
        dom.tags(block)
            .filter(|&id| dom.name(id) == "a" && dom.attr(id, "href").is_some()),
    );
    let mut roots = HashSet::new();
    let mut output = Vec::new();
    for link in links {
        let raw = dom.attr(link, "href").unwrap_or("");
        if strip(raw).is_empty() {
            continue;
        }
        let resolved = resolve(href, raw);
        if resolved.external {
            continue;
        }
        let mut wrapper = None;
        for parent in dom.parents(link) {
            if is_super(dom, parent) {
                if hint(dom, link, parent, &dom.text(parent, "", true), true) {
                    wrapper = Some(parent);
                }
                break;
            }
            if parent == block {
                break;
            }
        }
        let marker = if wrapper.is_none() {
            trailing_note_marker(dom, link)
        } else {
            None
        };
        let mut body = String::new();
        let mut stack: Vec<_> = dom.children(link).iter().rev().copied().collect();
        while let Some(node) = stack.pop() {
            match dom.kind(node) {
                Kind::Text(s) => body.push_str(s),
                Kind::Tag(name, _)
                    if Some(node) != marker && !matches!(name.as_str(), "rt" | "rp") =>
                {
                    stack.extend(dom.children(node).iter().rev().copied())
                }
                _ => {}
            }
        }
        let body = SPACE.replace_all(&body, " ");
        let body = strip(&body);
        if body.is_empty() && wrapper.is_none() {
            continue;
        }
        let marker_only = MARKER.is_match(body)
            && (hint(dom, link, link, body, false) || inside_implicit(dom, link, body));
        let point = marker_only || wrapper.is_some();
        let node = wrapper.unwrap_or(link);
        if !roots.insert(node) {
            continue;
        }
        let id = format!("{anchor}_annotation_{}", output.len());
        output.push(Annotation {
            node,
            id: id.clone(),
            marker,
            point,
            marker_text: if point {
                dom.text(node, "", true)
            } else {
                marker.map_or_else(String::new, |n| dom.text(n, "", true))
            },
            href: raw.to_owned(),
            target_key: resolved.key(),
            relation: relation(
                dom,
                link,
                block,
                marker_only,
                wrapper.is_some(),
                marker.is_some(),
                &dom.text(link, "", true),
            ),
        });
        dom.set(node, ANNOTATION, id);
    }
    output
}

fn empty_roots(dom: &Dom, block_id: Id) -> HashSet<Id> {
    let mut roots = HashSet::new();
    for id in dom.tags(block_id) {
        let name = dom.name(id);
        if !atomic(name)
            && !(matches!(name, "a" | "span")
                && (dom.attr(id, "id").is_some() || dom.attr(id, "name").is_some())
                && !dom.meaningful(id))
        {
            continue;
        }
        if dom.attr(id, ANNOTATION).is_some()
            || dom
                .parents(id)
                .any(|p| p != block_id && atomic(dom.name(p)))
        {
            continue;
        }
        let mut root = id;
        for p in dom.parents(id) {
            if p == block_id
                || block(dom.name(p))
                || dom.attr(p, ANNOTATION).is_some()
                || dom.meaningful(p)
            {
                break;
            }
            root = p;
        }
        roots.insert(root);
    }
    roots
}

#[derive(Default)]
struct Content {
    raw: String,
    objects: Vec<(Id, usize)>,
    events: HashMap<Id, (usize, usize)>,
}

enum Event {
    Enter(Id, bool),
    EndRange(Id, usize),
    EndRuby(Id, usize),
}

fn content(dom: &Dom, el: Id, annotations: &[Annotation]) -> Content {
    let atomic = empty_roots(dom, el);
    let by_node: HashMap<_, _> = annotations.iter().map(|a| (a.node, a)).collect();
    let markers: HashSet<_> = annotations.iter().filter_map(|a| a.marker).collect();
    let mut c = Content::default();
    let mut stack = vec![Event::Enter(el, false)];
    while let Some(event) = stack.pop() {
        match event {
            Event::EndRange(id, start) => {
                c.events.insert(id, (start, c.raw.len()));
            }
            Event::EndRuby(id, start) => {
                if c.raw.len() > start {
                    let text = dom
                        .tags(id)
                        .filter(|&n| dom.name(n) == "rt")
                        .map(|n| dom.text(n, "", false))
                        .collect::<String>();
                    if !strip(&text).is_empty() {
                        c.raw.push('〘');
                        c.raw.push_str(strip(&text));
                        c.raw.push('〙');
                    }
                }
            }
            Event::Enter(id, mut in_range) => match dom.kind(id) {
                Kind::Tag(name, _) => {
                    if matches!(name.as_str(), "rt" | "rp") || in_range && markers.contains(&id) {
                        continue;
                    }
                    if name == "ruby" {
                        stack.push(Event::EndRuby(id, c.raw.len()));
                    } else if let Some(a) = by_node.get(&id) {
                        let start = c.raw.len();
                        if a.point {
                            c.events.insert(id, (start, start));
                            continue;
                        }
                        stack.push(Event::EndRange(id, start));
                        in_range = true;
                    } else if atomic.contains(&id) {
                        c.objects.push((id, c.raw.len()));
                        continue;
                    }
                    stack.extend(
                        dom.children(id)
                            .iter()
                            .rev()
                            .map(|&n| Event::Enter(n, in_range)),
                    );
                }
                Kind::Text(text) => {
                    let mut text = text.as_str();
                    if in_range && !markers.is_empty() {
                        if let Some(parent) = dom.parent(id) {
                            let children = dom.children(parent);
                            let pos = children.iter().position(|&n| n == id).unwrap();
                            if children.get(pos + 1).is_some_and(|n| markers.contains(n)) {
                                text = text.trim_end_matches(|c| {
                                    matches!(c, ' ' | '\t' | '\n' | '\r' | '\x0b' | '\x0c')
                                });
                            }
                            if strip(text).is_empty()
                                && pos > 0
                                && markers.contains(&children[pos - 1])
                                && !children[pos + 1..].iter().any(|&n| visible(dom, n))
                            {
                                continue;
                            }
                        }
                    }
                    c.raw.push_str(text);
                }
                _ => {}
            },
        }
    }
    c
}

pub(crate) fn normalize(raw: &str, offsets: &[usize]) -> (String, Vec<usize>) {
    if offsets.is_empty() {
        return (strip(&SPACE.replace_all(raw, " ")).to_owned(), vec![]);
    }
    let mut collapsed = String::with_capacity(raw.len());
    let mut map = vec![0; raw.len() + 1];
    let mut length = 0;
    for (idx, ch) in raw.char_indices() {
        map[idx] = length;
        let whitespace = matches!(ch, ' ' | '\t' | '\n' | '\r' | '\x0b' | '\x0c');
        if !whitespace || !collapsed.ends_with(' ') {
            collapsed.push(if whitespace { ' ' } else { ch });
            length += 1;
        }
    }
    map[raw.len()] = length;
    let stripped = strip(&collapsed);
    let left =
        collapsed.trim_start_matches(|c: char| c.is_whitespace() || matches!(c, '\x1c'..='\x1f'));
    let leading = length - left.chars().count();
    let limit = stripped.chars().count();
    (
        stripped.to_owned(),
        offsets
            .iter()
            .map(|&n| map[n].saturating_sub(leading).min(limit))
            .collect(),
    )
}

pub fn annotate_with_schema(
    markup: &str,
    index: usize,
    href: &str,
    book_title: &str,
    skip_nav: bool,
    loose_text: bool,
) -> Resource {
    annotate_dom_with_schema(
        Dom::parse(markup),
        index,
        href,
        book_title,
        skip_nav,
        loose_text,
    )
}

pub(crate) fn annotate_dom_with_schema(
    mut dom: Dom,
    index: usize,
    href: &str,
    book_title: &str,
    skip_nav: bool,
    loose_text: bool,
) -> Resource {
    let targets = targets(&mut dom, skip_nav, loose_text);
    let mut segments = Vec::with_capacity(targets.len());
    let mut first_heading = None;
    let mut title = String::new();
    for el in targets {
        let anchor = format!("tn{index}_{}", segments.len());
        let annotations = annotations(&mut dom, el, &anchor, href);
        let mut protected = HashSet::new();
        let range_roots: HashSet<_> = annotations
            .iter()
            .filter(|a| !a.point)
            .map(|a| a.node)
            .collect();
        for a in &annotations {
            protected.insert(a.node);
            if a.point {
                protected.extend(dom.tags(a.node));
            }
            if let Some(marker) = a.marker {
                protected.insert(marker);
                protected.extend(dom.tags(marker));
            }
        }
        let markers: Vec<_> = dom
            .tags(el)
            .filter(|&id| {
                (dom.attr(id, "id").is_some() || dom.attr(id, "name").is_some())
                    && !atomic(dom.name(id))
                    && !protected.contains(&id)
                    && !dom.parents(id).any(|p| p != el && atomic(dom.name(p)))
                    && dom.meaningful(id)
            })
            .collect();
        for id in markers {
            let inside_link = dom
                .parents(id)
                .any(|p| range_roots.contains(&p) || dom.name(p) == "a");
            let marker = dom.tag(if inside_link { "span" } else { "a" });
            for key in ["id", "name"] {
                if let Some(s) = dom.attrs_mut(id).remove(key) {
                    dom.set(marker, key, s);
                }
            }
            dom.insert(id, marker, false);
        }
        let c = content(&dom, el, &annotations);
        let mut offsets: Vec<_> = c.objects.iter().map(|&(_, pos)| pos).collect();
        offsets.extend(annotations.iter().flat_map(|a| {
            let (s, e) = c
                .events
                .get(&a.node)
                .copied()
                .unwrap_or((c.raw.len(), c.raw.len()));
            [s, e]
        }));
        let (text, offsets) = normalize(&c.raw, &offsets);
        if text.is_empty()
            || !annotations.is_empty()
                && annotations.iter().all(|a| a.point)
                && MARKER.is_match(&text)
        {
            continue;
        }

        let mut meta = json!({});
        if !offsets.is_empty() {
            let length = text.chars().count();
            let mut nodes = Vec::new();
            let mut items = Vec::new();
            let positions: HashMap<_, _> = dom
                .walk(el)
                .enumerate()
                .map(|(pos, n)| (n, pos + 1))
                .collect();
            for (i, a) in annotations.iter().enumerate() {
                let start = offsets[c.objects.len() + 2 * i];
                let end = offsets[c.objects.len() + 2 * i + 1];
                let source_text: String = text.chars().skip(start).take(end - start).collect();
                items.push((positions.get(&a.node).copied().unwrap_or(0), json!({
                    "id": a.id, "mode": if a.point { "point" } else { "range" },
                    "source_start": start, "source_end": end, "source_text": source_text,
                    "marker_text": a.marker_text, "raw_href": a.href, "target_key": a.target_key, "relation": a.relation
                })));
            }
            for (i, &(node, _)) in c.objects.iter().enumerate() {
                let id = format!("{anchor}_inline_{i}");
                let offset = offsets[i];
                dom.set(node, INLINE, id.clone());
                let placement = if offset == 0 {
                    "before"
                } else if offset == length {
                    "after"
                } else {
                    "inline"
                };
                nodes.push(json!({"id": id, "tag": dom.name(node), "placement": placement, "offset": offset}));
                if placement != "inline" && !dom.meaningful(node) {
                    continue;
                }
                dom.set(node, ANNOTATION, id.clone());
                let mut token_id = id.clone();
                let mut token = format!("〘object:{token_id}〙");
                while text.contains(&token) {
                    token_id.push_str("_x");
                    token = format!("〘object:{token_id}〙");
                }
                let mut object_source = dom.text(node, " ", true);
                if object_source.is_empty() {
                    object_source = dom.attr(node, "src").unwrap_or(dom.name(node)).to_owned();
                }
                items.push((positions[&node], json!({
                    "id": id, "mode": "point", "source_start": offset, "source_end": offset,
                    "source_text": "", "marker_text": "", "raw_href": "", "target_key": id,
                    "relation": OBJECT, "marker": token, "object_source": object_source, "object_tag": dom.name(node)
                })));
            }
            if !nodes.is_empty() {
                meta["epub_inline"] =
                    json!({"version": 1, "source_length": length, "nodes": nodes});
            }
            if !items.is_empty() {
                items.sort_by_key(|(position, _)| *position);
                meta["epub_annotations"] = json!({"version": 1, "source_length": length, "items": items.into_iter().map(|(_, v)| v).collect::<Vec<_>>()});
            }
        }
        dom.set(el, ANCHOR, anchor.clone());
        let instructions: Vec<_> = dom
            .walk(el)
            .filter(|&n| matches!(dom.kind(n), Kind::Pi(_)))
            .collect();
        for n in instructions {
            dom.insert(el, n, false);
        }
        let h = scope(&dom, el, |id| heading(dom.name(id)));
        if let Some(h) = h {
            if *first_heading.get_or_insert(h) == h {
                if !title.is_empty() {
                    title.push(' ');
                }
                title.push_str(&text);
            }
        }
        segments.push(Segment {
            index: segments.len(),
            source: text,
            kind: if h.is_some() { "heading" } else { "text" },
            anchor,
            resource_href: href.to_owned(),
            meta,
            node: el,
        });
    }
    if title.is_empty() {
        if let Some(el) = dom.find(0, "title") {
            let mut node = el;
            while let [only] = dom.children(node) {
                node = *only;
            }
            if let Kind::Text(text) = dom.kind(node) {
                let text = strip(text);
                let base = href.rsplit('/').next().unwrap_or("");
                let base = base.rsplit_once('.').map_or(base, |(stem, _)| stem);
                if text != base && text != strip(book_title) {
                    title = text.to_owned();
                }
            }
        }
    }
    Resource {
        dom,
        title,
        segments,
    }
}

pub struct FragmentIndex<'a> {
    pub owners: HashMap<&'a str, Vec<Id>>,
    pub boundaries: HashMap<&'a str, Option<Id>>,
    next: HashMap<Id, Id>,
}

impl<'a> FragmentIndex<'a> {
    pub fn new(dom: &'a Dom) -> Self {
        let mut owners: HashMap<_, Vec<_>> = HashMap::new();
        let mut next = HashMap::new();
        let mut pending = Vec::new();
        for id in dom.tags(0) {
            if dom.attr(id, ANCHOR).is_some() {
                for n in pending.drain(..) {
                    next.insert(n, id);
                }
            }
            let name = dom.attr(id, "name");
            let identity = dom.attr(id, "id");
            if name.is_none() && identity.is_none() {
                continue;
            }
            pending.push(id);
            for key in [identity, name.filter(|&n| Some(n) != identity)]
                .into_iter()
                .flatten()
                .filter(|s| !s.is_empty())
            {
                owners.entry(key).or_default().push(id);
            }
        }
        let boundaries = owners
            .iter()
            .map(|(&key, nodes)| {
                let node = nodes[0];
                (
                    key,
                    scope(dom, node, |n| dom.attr(n, ANCHOR).is_some())
                        .or(next.get(&node).copied()),
                )
            })
            .collect();
        Self {
            owners,
            boundaries,
            next,
        }
    }

    pub fn note_scope(&self, dom: &Dom, node: Id, fragment: &str) -> (Id, bool) {
        let has_anchor = |id| {
            dom.attr(id, ANCHOR).is_some() || dom.tags(id).any(|n| dom.attr(n, ANCHOR).is_some())
        };
        if let Some(n) = semantic_scope(dom, node) {
            return (n, true);
        }
        if let Some(n) = implicit_scope(dom, node).filter(|&n| has_anchor(n)) {
            return (n, true);
        }
        if let Some(n) = short_scope(dom, node).filter(|&n| has_anchor(n)) {
            return (n, false);
        }
        let kind = dom.name(node);
        if (matches!(kind, "aside" | "li" | "dd")
            || matches!(kind, "div" | "section")
                && (NOTE.is_match(fragment) || has_note_id(dom, node)))
            && has_anchor(node)
        {
            return (node, false);
        }
        (
            scope(dom, node, |n| dom.attr(n, ANCHOR).is_some())
                .or(self.next.get(&node).copied())
                .unwrap_or(node),
            false,
        )
    }
}
