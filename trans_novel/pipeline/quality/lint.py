"""确定性译文 lint：机器可判定的硬伤（引号丢失/数字失配/锁定专名漂移/未译残留/
长度异常），零 LLM、零 IO，纯函数。Repair 节点消费完整 issue 清单并负责模型修复。

原则：宁漏勿误报——每个校验器都保守，只抓确凿证据；不确定的一律放过，交给
Repair agent 去判断语义类问题。阈值/规则均以两本已交付书的真实数据回测校准过。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from trans_novel.agents import langprofile
from trans_novel.glossary.store import GlossaryTerm
from trans_novel.pipeline.quality.checks import length_flags

# ── LintIssue ────────────────────────────────────────────────────────────
ISSUE_QUOTE_LOSS = "quote_loss"
ISSUE_NUMBER_MISMATCH = "number_mismatch"
ISSUE_TERM_MISS = "term_miss"
ISSUE_UNTRANSLATED = "untranslated"
ISSUE_EMPTY = "empty"
ISSUE_TOO_SHORT = "too_short"
ISSUE_TOO_LONG = "too_long"


@dataclass
class LintIssue:
    index: int
    type: str
    detail: str


# ── a) quote_loss ───────────────────────────────────────────────────────
# 实测：源段行首若是残段闭引号（句段切分产生的孤立后半句）会大量误报，故收窄为
# "源段 strip 后以开引号起始"才算"含直接引语"的证据；闭引号不算数。
# 译侧除中文引号外，书名号《》也算保留（引题名转书名号是正确译法，不算丢引号）。
_OPEN_QUOTE_CHARS = '“"「『«'
_TGT_QUOTE_CHARS = "“”「」『』《》"


def _has_quote_loss(source: str, target: str) -> bool:
    src = source.strip()
    if not src or src[0] not in _OPEN_QUOTE_CHARS:
        return False
    tgt = (target or "").strip()
    return not any(ch in tgt for ch in _TGT_QUOTE_CHARS)


_PAIRED_DIALOGUE_QUOTES = (("“", "”"), ("「", "」"), ("『", "』"), ("«", "»"))


def _dialogue_quote_pair_count(text: str) -> int:
    return text.count('"') // 2 + sum(
        min(text.count(opening), text.count(closing))
        for opening, closing in _PAIRED_DIALOGUE_QUOTES
    )


def drops_dialogue_quotes(source: str, current: str, proposal: str) -> bool:
    """Return whether a rewrite removes dialogue boundaries preserved by the current target."""
    required = min(_dialogue_quote_pair_count(source), _dialogue_quote_pair_count(current))
    return _dialogue_quote_pair_count(proposal) < required


# ── b) number_mismatch ──────────────────────────────────────────────────
_EN_UNITS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}
_EN_TENS = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
# "hundred" 通过下方 eight-hundred 组合分支专门处理，不放进通用乘数表（避免误把
# 十位词后紧跟的 "hundred" 二次放大）；grand=1000 口语数字俗语（five grand → 5000）；
# decade(s) 按 ×10 等价组合（four decades → 40，用于匹配译侧"四十年"）。
_EN_MULT = {
    "thousand": 1000,
    "million": 1_000_000,
    "billion": 1_000_000_000,
    "trillion": 1_000_000_000_000,
    "grand": 1000,
    "decade": 10,
    "decades": 10,
}

_ARABIC_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_EN_WORD_RE = re.compile(r"[A-Za-z]+(?:-[A-Za-z]+)?")
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
_CN_DIGIT = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000, "万": 10_000, "亿": 100_000_000}
_CN_NUM_RE = re.compile(r"[零一二两三四五六七八九十百千万亿]+")
_CN_WILDCARD_YEAR_RE = re.compile(r"[零一二三四五六七八九]{2,}几")
_CN_TENTH_RE = re.compile(r"[一二三四五六七八九]成")
_TGT_SCALE = {"千": 1000, "万": 10_000, "亿": 100_000_000, "万亿": 1_000_000_000_000}
_TGT_SCALE_RE = re.compile(r"[ \t\u3000]*(万亿|[千万亿])")
_RANGE_CONNECTOR_RE = re.compile(r"\s*(to|-|~|–|—)\s*\$?\s*", re.I)


def _is_glued_identifier(text: str, start: int, end: int) -> bool:
    """数字紧贴字母（无空格/标点分隔）多为版式伪影或字母数字标识符。"""
    after = text[end : end + 1]
    if after.isalpha():
        return True
    before = text[max(0, start - 1) : start]
    if before.isalpha():
        return True
    return bool(before == "-" and start >= 2 and text[start - 2].isalpha())


def _extract_arabic_with_multiplier(text: str) -> set[float]:
    """阿拉伯数字 + 紧随乘数词（thousand/million/billion/grand/decade(s)）组合成
    单值（11.8 billion → 1.18e10）；"A to/- B <乘数>" 范围表达式对 A 也套用同一
    乘数（$30 to $50 billion 各自组合为 3e10/5e10）——乘数只认紧邻的下一个词，
    不跨词/跨句查找。4 位年份（1000-2199）永不参与组合（独立成值，防止像
    "...$500,000 in 1958 to $21 million..." 里的年份被范围表达式误套乘数）。
    字母粘连的数字（版式伪影/标识符）不提取。"""
    matches = []  # [start, end, raw_value, mult_factor, is_year]
    for m in _ARABIC_RE.finditer(text):
        if _is_glued_identifier(text, m.start(), m.end()):
            continue
        try:
            raw = float(m.group(0).replace(",", ""))
        except ValueError:
            continue
        is_year = raw == int(raw) and 1000 <= raw <= 2199 and "." not in m.group(0)
        mult = 1.0
        if not is_year:
            after = text[m.end() : m.end() + 20]
            w = re.match(r"\s*([A-Za-z]+)", after)
            if w and w.group(1).lower() in _EN_MULT:
                mult = float(_EN_MULT[w.group(1).lower()])
        matches.append([m.start(), m.end(), raw, mult, is_year])
    for j in range(len(matches) - 1):
        a, b = matches[j], matches[j + 1]
        if a[4] or b[4]:
            continue  # 年份不参与范围表达式的乘数回填
        gap = text[a[1] : b[0]]
        if a[3] == 1.0 and b[3] != 1.0 and _RANGE_CONNECTOR_RE.fullmatch(gap):
            a[3] = b[3]
    return {a[2] * a[3] for a in matches}


def _extract_english_number_words(text: str) -> set[float]:
    """英文数词提取：十位表 + hyphenated 组合 + thousand/million/billion/grand/
    decade(s) 乘数；"X hundred (and) Y" 组合成单值（X∈one..nineteen，覆盖
    "fifteen hundred"→1500、"eight hundred and thirty-six"→836），组合消费掉的
    分量不再单独计入值集合。不做更复杂组合（序数词不解析）。"""
    tokens = [t.lower() for t in _EN_WORD_RE.findall(text)]
    values: set[float] = set()
    n = len(tokens)
    i = 0

    def _small(tok: str) -> int | None:
        if "-" in tok:
            a, b = tok.split("-", 1)
            if a in _EN_TENS and b in _EN_UNITS:
                return _EN_TENS[a] + _EN_UNITS[b]
            return None
        if tok in _EN_UNITS:
            return _EN_UNITS[tok]
        if tok in _EN_TENS:
            return _EN_TENS[tok]
        return None

    while i < n:
        tok = tokens[i]
        val: int | None = None
        consumed = 1
        if tok in _EN_UNITS and i + 1 < n and tokens[i + 1] == "hundred":
            val = _EN_UNITS[tok] * 100
            j = i + 2
            if j < n and tokens[j] == "and":
                j += 1
            if j < n:
                add = _small(tokens[j])
                if add is not None:
                    val += add
                    j += 1
            consumed = j - i
        else:
            val = _small(tok)
        if val is None:
            i += 1
            continue
        j = i + consumed
        while j < n and tokens[j] in _EN_MULT:
            val *= _EN_MULT[tokens[j]]
            j += 1
        values.add(float(val))
        i = j
    return values


def _extract_source_numbers(text: str, src_lang: str) -> set[float]:
    if src_lang == "en":
        return _extract_arabic_with_multiplier(text) | _extract_english_number_words(text)
    values: set[float] = set()
    for m in _ARABIC_RE.finditer(text):
        if _is_glued_identifier(text, m.start(), m.end()):
            continue
        try:
            values.add(float(m.group(0).replace(",", "")))
        except ValueError:
            continue
    return values


def _cn_to_number(s: str) -> float | None:
    """位值汉字数字解析，优先识别复合单位 ``万亿``。"""
    if not s:
        return None
    if "万亿" in s:
        prefix, suffix = s.split("万亿", 1)
        prefix_value = _cn_to_number(prefix) if prefix else 1.0
        if prefix_value is None:
            return None
        if suffix:
            tail = _cn_to_number(suffix)
            if tail is None:
                return None
            prefix_value += tail / 1_000_000_000_000
        return prefix_value * 1_000_000_000_000
    total = 0
    section = 0
    num = 0
    seen_digit_or_unit = False
    for ch in s:
        if ch in _CN_DIGIT:
            num = _CN_DIGIT[ch]
            seen_digit_or_unit = True
        elif ch in _CN_UNIT:
            unit = _CN_UNIT[ch]
            seen_digit_or_unit = True
            if unit >= 10_000:
                section = (section + num) * unit
                total += section
                section = 0
            else:
                section += (num or 1) * unit
            num = 0
        else:
            return None
    if not seen_digit_or_unit:
        return None
    total += section + num
    return float(total)


def _cn_digits_literal(s: str) -> float | None:
    """纯数字汉字串（不含十百千万等位值字符）按位读，如 一九二二→1922。"""
    if len(s) < 2 or any(ch in _CN_UNIT for ch in s):
        return None
    digits = "".join(str(_CN_DIGIT[ch]) for ch in s if ch in _CN_DIGIT)
    if len(digits) < 2:
        return None
    return float(int(digits))


def _extract_target_numbers(text: str) -> set[float]:
    values: set[float] = set()
    norm = text.translate(_FULLWIDTH_DIGITS)
    for m in _ARABIC_RE.finditer(norm):
        try:
            v = float(m.group(0).replace(",", ""))
        except ValueError:
            continue
        after = norm[m.end() : m.end() + 3]
        scale_m = _TGT_SCALE_RE.match(after)
        if scale_m:
            v *= _TGT_SCALE[scale_m.group(1)]
        values.add(v)
    for m in _CN_NUM_RE.finditer(norm):
        s = m.group(0)
        if any(ch in _CN_UNIT for ch in s):
            v = _cn_to_number(s)
        else:
            v = _cn_digits_literal(s) or (_cn_to_number(s) if len(s) == 1 else None)
        if v is not None:
            values.add(v)
    if "一半" in norm:
        values.add(50.0)
    for m in _CN_TENTH_RE.finditer(norm):
        values.add(float(_CN_DIGIT[m.group(0)[0]] * 10))
    return values


def _extract_target_wildcard_year_prefixes(text: str) -> list[str]:
    """ "一九四几" 这类通配年份：提取已知前缀数字串（≥3 位才有判定意义）。"""
    norm = text.translate(_FULLWIDTH_DIGITS)
    prefixes = []
    for m in _CN_WILDCARD_YEAR_RE.finditer(norm):
        digits = "".join(str(_CN_DIGIT[ch]) for ch in m.group(0) if ch in _CN_DIGIT)
        if len(digits) >= 3:
            prefixes.append(digits)
    return prefixes


def _fmt_num(v: float) -> str:
    return str(int(v)) if v == int(v) else str(v)


def _number_mismatch_detail(
    src_values: set[float], tgt_values: set[float], tgt_wildcard_prefixes: list[str]
) -> str | None:
    """判定：仅当源侧某值 v≥4 且不在译侧值集合（容忍 ±0.5% 缩放误差）→ 报告。
    译侧多出的值不 flag（意译合法）。年代/世纪等价：v∈[1000,2199] 时，译侧出现
    v%100（如 1980→80）、v//100（19）或 v//100+1（20，"世纪"进位）任一即视为匹配
    （1980s→"20世纪80年代"✓、1600s→十七世纪✓）；或译侧通配年份前缀命中（一九四几
    命中 1943）。"""
    missing = []
    for v in sorted(src_values):
        if v < 4:
            continue
        if any(abs(v - tv) <= max(v, 1) * 0.005 for tv in tgt_values):
            continue
        if 1000 <= v <= 2199:
            vi = int(v)
            candidates = {vi % 100, vi // 100, vi // 100 + 1}
            if candidates & tgt_values:
                continue
            if any(str(vi).startswith(p) for p in tgt_wildcard_prefixes if len(p) >= 3):
                continue
        missing.append(v)
    if not missing:
        return None
    parts = "、".join(_fmt_num(v) for v in missing)
    detail = f"原文数值 {parts} 未在译文中出现，请核对数字后重译"
    if tgt_values:
        detail += f"——译文出现了 {'、'.join(_fmt_num(v) for v in sorted(tgt_values))}"
    return detail


# ── c) term_miss（仅 locked 术语） ───────────────────────────────────────
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff]")


def _term_matches_source(term_source: str, source: str) -> bool:
    if not term_source:
        return False
    if _CJK_RE.search(term_source):
        return term_source in source
    return re.search(r"\b" + re.escape(term_source) + r"\b", source) is not None


_LATIN_SINGLE_TOKEN_RE = re.compile(r"[A-Za-z]+")
# 句首位置：字符串开头，或句末标点（.!?…）和空白之后。句末标点与空白之间可以出现闭引号
# 或闭括号；空白之后可以紧跟开引号或开括号。只有紧邻这两类锚点的引号或括号才属于句首；
# 句中开引号（如 said, “Law …）不算句首。
_SENTENCE_INITIAL_RE = re.compile(r"(?:^|[.!?…][\u201d\u2019\"')）]*\s+)[\u201c\u2018\"'（(]*\s*$")


def _partial_target_hit(t_target: str, target: str) -> bool:
    """人工分析剩余的 129 条 term_miss 后发现，其中 72 条（56%）是译文合理地只使用了
    部分译名（如锁定「乔赛亚·蔡尔德」，译文只写「蔡尔德」）。将 t_target 按 ·、• 或空白
    拆分；若译文中出现任一长度不小于 2 且不等于完整译名的部分，则视为已翻译。无法拆分
    的 t_target（如「杜鲁门」）沿用原逻辑，仍须完整匹配。
    """
    for part in re.split(r"[·•\s]+", t_target):
        if len(part) >= 2 and part != t_target and part in target:
            return True
    return False


def _only_sentence_initial_matches(t_source: str, source: str) -> bool:
    """E2 句首歧义单词豁免：对剩余 129 条结果进行人工分析后发现，其中 14 条源于误录为
    人名的条目命中了句首大写的普通英文单词（如 Any→安尼、Law→约翰·劳）。仅当 t_source
    为纯拉丁字母单个 token、且源文中所有词边界命中都位于句首时才豁免；只要有一处在非
    句首命中，仍按常规检查——例如 “...and the Law of Unintended Consequences” 中的
    Law 仍会被报告，并按真实问题处理。
    """
    if not _LATIN_SINGLE_TOKEN_RE.fullmatch(t_source):
        return False
    matches = list(re.finditer(r"\b" + re.escape(t_source) + r"\b", source))
    if not matches:
        return False
    return all(_SENTENCE_INITIAL_RE.search(source[: m.start()]) for m in matches)


def _term_miss_details(source: str, target: str, locked_terms) -> list[str]:
    details = []
    for term in locked_terms:
        t_source = getattr(term, "source", "")
        t_target = getattr(term, "target", "")
        if not t_target:
            continue
        if not _term_matches_source(t_source, source):
            continue
        if t_target in (target or ""):
            continue
        if _partial_target_hit(t_target, target or ""):
            continue
        if _only_sentence_initial_matches(t_source, source):
            continue
        details.append(
            f"锁定术语「{t_source}」应译为「{t_target}」，译文未出现该译名，请核对后重译"
        )
    return details


# ── d) untranslated ─────────────────────────────────────────────────────
_LATIN_RUN_RE = re.compile(r"[A-Za-z]{40,}")


def _norm_for_compare(text: str) -> str:
    return re.sub(r"\s+", "", text or "").lower()


def _is_untranslated(source: str, target: str) -> bool:
    if not any(character.isalpha() for character in source) or langprofile.is_machine_literal(
        source
    ):
        return False
    ns, nt = _norm_for_compare(source), _norm_for_compare(target)
    if ns and ns == nt:
        return True
    return any(m.group(0) in source for m in _LATIN_RUN_RE.finditer(target or ""))


# ── 入口 ─────────────────────────────────────────────────────────────────
_LENGTH_DETAIL = {
    "empty": "译文为空，疑似漏译，请补全",
    "too_short": "译文明显过短，疑似漏译，请核对补全",
    "too_long": "译文明显过长，疑似译文失控或误增译，请核对精简",
}
# en→zh 合法压缩比波动实测极大（1230 处 too_short 误报），额外加硬门槛：
# 只有比值和绝对长度都足够极端才有判定意义。
_EN_TOO_SHORT_RATIO = 0.15
_EN_TOO_SHORT_MIN_SRC_LEN = 120


def lint_targets(
    sources: list[str],
    targets: list[str],
    *,
    locked_terms: list[GlossaryTerm] | tuple[GlossaryTerm, ...] = (),
    src_lang: str = "en",
) -> list[LintIssue]:
    """对一批 (source, target) 跑全部确定性校验，返回按段落顺序的 issue 列表。

    全部保守：宁漏勿误报。locked_terms 传入 locked=1 的 GlossaryTerm（人物术语）；
    src_lang 用于决定是否启用英文数词/未译判定（"zh" 源不启用未译判定）。
    """
    issues: list[LintIssue] = []
    for lf in length_flags(sources, targets):
        if lf.reason == "too_short" and src_lang == "en":
            s_len = len(sources[lf.index].strip())
            if not (lf.ratio < _EN_TOO_SHORT_RATIO and s_len >= _EN_TOO_SHORT_MIN_SRC_LEN):
                continue
        issues.append(LintIssue(lf.index, lf.reason, _LENGTH_DETAIL[lf.reason]))

    for i, (s, t) in enumerate(zip(sources, targets, strict=False)):
        t = t or ""
        if _has_quote_loss(s, t):
            issues.append(
                LintIssue(
                    i,
                    ISSUE_QUOTE_LOSS,
                    "原文含直接引语，译文丢失引号，必须保留成对引号",
                )
            )

        nm_detail = _number_mismatch_detail(
            _extract_source_numbers(s, src_lang),
            _extract_target_numbers(t),
            _extract_target_wildcard_year_prefixes(t),
        )
        if nm_detail:
            issues.append(LintIssue(i, ISSUE_NUMBER_MISMATCH, nm_detail))

        for detail in _term_miss_details(s, t, locked_terms):
            issues.append(LintIssue(i, ISSUE_TERM_MISS, detail))

        if src_lang != "zh" and _is_untranslated(s, t):
            issues.append(
                LintIssue(
                    i,
                    ISSUE_UNTRANSLATED,
                    "译文与原文高度重合，疑似整段未译，请重新翻译",
                )
            )

    return issues


@dataclass(frozen=True, slots=True)
class PolishGateResult:
    proposal: str
    selected: str
    accepted: bool
    rejection_reasons: tuple[str, ...] = ()
    raw_issues: list[LintIssue] = field(default_factory=list)
    proposal_issues: list[LintIssue] = field(default_factory=list)
    checked_raw: str = ""
    checked_proposal: str = ""


def polish_gate(
    source: str,
    raw: str,
    proposal: str,
    *,
    locked_terms: list[GlossaryTerm] | tuple[GlossaryTerm, ...] = (),
    src_lang: str = "en",
    normalize_punctuation: bool = True,
) -> PolishGateResult:
    """Apply the production polish lint gate to one segment."""
    from trans_novel.postprocess.punct import normalize_zh

    raw_normalized = normalize_zh(raw) if normalize_punctuation and raw else raw
    proposed = normalize_zh(proposal) if normalize_punctuation and proposal else proposal
    raw_issues = lint_targets(
        [source], [raw_normalized], locked_terms=locked_terms, src_lang=src_lang
    )
    proposal_issues = lint_targets(
        [source], [proposed], locked_terms=locked_terms, src_lang=src_lang
    )
    introduced = tuple(
        sorted({issue.type for issue in proposal_issues} - {issue.type for issue in raw_issues})
    )
    if introduced:
        return PolishGateResult(
            proposal=proposed,
            selected=raw_normalized,
            accepted=False,
            rejection_reasons=introduced,
            raw_issues=raw_issues,
            proposal_issues=proposal_issues,
            checked_raw=raw_normalized,
            checked_proposal=proposed,
        )
    return PolishGateResult(
        proposal=proposed,
        selected=proposed,
        accepted=True,
        raw_issues=raw_issues,
        proposal_issues=proposal_issues,
        checked_raw=raw_normalized,
        checked_proposal=proposed,
    )


evaluate_polish_gate = polish_gate
