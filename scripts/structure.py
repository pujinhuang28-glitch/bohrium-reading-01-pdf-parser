"""结构层：标题、作者、摘要、章节层级、公式、图表题注、参考文献。

只做「能从版面和文字里稳定判出来」的事；判不出来的进 uncertainty，不猜。
"""
from __future__ import annotations

import re
from collections import Counter

from layout import Block

SECTION_NUM = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,3})\.?\s+(\S.{0,120})$")
SECTION_WORD = re.compile(
    r"^(abstract|introduction|related work|background|method(?:s|ology)?|approach|"
    r"experiments?|results?|discussion|conclusions?|references|bibliography|"
    r"acknowledge?ments?|appendix|limitations?)\b", re.I)
CAP_FIG = re.compile(r"^(?:figure|fig\.)\s*(\d+[a-z]?)", re.I)
CAP_TAB = re.compile(r"^table\s*(\d+[a-z]?)", re.I)
EQ_NUM = re.compile(r"\((\d{1,3}[a-z]?)\)\s*$")


def body_size(blocks: list[Block]) -> float:
    c = Counter(round(b.size, 1) for b in blocks if len(b.text) > 60)
    return c.most_common(1)[0][0] if c else 10.0


def detect_title(blocks: list[Block], pages: int) -> tuple[str, str]:
    """首页最大字号、靠前的块当标题。"""
    first = [b for b in blocks if b.page == 1][:40]
    if not first:
        return "", "no blocks on page 1"
    top = max(first, key=lambda b: (round(b.size, 1), -b.bbox[1]))
    same = [b for b in first
            if abs(b.size - top.size) < 0.6 and b.bbox[1] < top.bbox[1] + top.size * 3.2
            and b.bbox[1] >= top.bbox[1] - 2]
    same.sort(key=lambda b: (b.bbox[1], b.bbox[0]))
    title = " ".join(b.text for b in same).strip()
    return title, "largest font on page 1"


def detect_abstract(blocks: list[Block], body: float) -> tuple[str, dict | None]:
    for i, b in enumerate(blocks):
        t = b.text.strip()
        if re.match(r"^abstract\b", t, re.I):
            text = re.sub(r"^abstract[\s:：.—-]*", "", t, flags=re.I)
            if len(text) < 40 and i + 1 < len(blocks):
                text = blocks[i + 1].text
            return text, {"page": b.page, "bbox": b.bbox}
        if re.match(r"^(a\s?b\s?s\s?t\s?r\s?a\s?c\s?t)$", t, re.I):
            if i + 1 < len(blocks):
                return blocks[i + 1].text, {"page": blocks[i + 1].page, "bbox": blocks[i + 1].bbox}
    # 没有 "Abstract" 字样：退而求其次，用首页第一个正文段
    for b in blocks:
        if b.page == 1 and abs(b.size - body) < 0.4 and len(b.text) > 200:
            return b.text, {"page": b.page, "bbox": b.bbox, "inferred": True}
    return "", None


def looks_like_heading(b: Block, body: float) -> bool:
    """标题的样子：短、以字母为主、不以句号结尾、不是数字串或表格行。"""
    t = b.text.strip()
    if not t or len(t) > 120:
        return False
    words = t.split()
    if len(words) > 14:
        return False
    letters = sum(ch.isalpha() for ch in t)
    digits = sum(ch.isdigit() for ch in t)
    if letters < len(t) * 0.55 or digits > len(t) * 0.25:
        return False
    if re.search(r"[=<>≤≥]", t):
        return False
    if re.fullmatch(r"[\d\s.,%/()\-–—]+", t):
        return False
    if t.endswith(".") and not SECTION_NUM.match(t):
        return False
    return b.size >= body * 0.98


def section_level(b: Block, body: float, prev_sizes: list[float],
                  table_bboxes: list[tuple[int, tuple]] | None = None) -> int | None:
    t = b.text.strip()
    if not t or len(t) > 160:
        return None
    if table_bboxes:
        for page, bb in table_bboxes:
            if page == b.page and not (b.bbox[2] < bb[0] or b.bbox[0] > bb[2]
                                       or b.bbox[3] < bb[1] or b.bbox[1] > bb[3]):
                return None
    if not looks_like_heading(b, body * 0.98):
        return None
    big = b.size >= body * 1.06
    m = SECTION_NUM.match(t)
    word = SECTION_WORD.match(t)
    if m and len(t) < 110:
        return m.group(1).count(".") + 1
    if word and (big or b.bold) and len(t) < 90:
        return 1
    if big and b.bold and not re.search(r"[.。]$", t):
        return 2
    return None


def detect_sections(blocks: list[Block], body: float,
                    table_bboxes: list[tuple[int, tuple]] | None = None) -> tuple[list[dict], list[dict]]:
    """返回 (sections, orphan_blocks)。"""
    # 首页标题/作者在 Abstract 之前，不能当章节
    start_idx = 0
    for i, b in enumerate(blocks):
        if re.match(r"^abstract\b", b.text.strip(), re.I):
            start_idx = i
            break

    heads: list[tuple[int, Block, int]] = []
    for i, b in enumerate(blocks):
        if i < start_idx:
            continue
        lvl = section_level(b, body, [x.size for x in blocks[:i]], table_bboxes)
        if lvl:
            heads.append((lvl, b, i))

    # 字号归一：一级标题应显著大于正文
    sections: list[dict] = []
    refs_start = None
    for idx, (lvl, b, i) in enumerate(heads):
        if re.match(r"^(references|bibliography)\b", b.text.strip(), re.I):
            refs_start = i
            break
    end_limit = refs_start if refs_start is not None else len(blocks)

    body_blocks: list[Block] = []
    for h in heads:
        if h[2] >= end_limit:
            continue
        sections.append({
            "id": "",
            "title": h[1].text.strip(),
            "level": min(h[0], 3),
            "page": h[1].page,
            "bbox": [round(v, 2) for v in h[1].bbox],
            "paragraphs": [],
        })
    # 按标题位置把正文块分配给最近的、位置在它之前的标题
    head_positions = [(h[2], si) for si, h in enumerate([x for x in heads if x[2] < end_limit])]
    for i, b in enumerate(blocks[:end_limit]):
        if any(i == h[2] for h in heads):
            continue
        owner = None
        for pos, si in head_positions:
            if pos < i:
                owner = si
            else:
                break
        if owner is not None and owner < len(sections):
            body_blocks.append(b)
            sections[owner]["paragraphs"].append({
                "id": f"{b.id}-p{len(sections[owner]['paragraphs']) + 1}",
                "text": b.text,
                "page": b.page,
                "bbox": [round(v, 2) for v in b.bbox],
                "size": round(b.size, 1),
            })
        elif owner is None:
            # 第一个标题之前是标题/作者一类的题头，不算任何章节
            body_blocks.append(b)
    for n, s in enumerate(sections, 1):
        s["id"] = f"sec-{n:02d}"
    return sections, [b for b in body_blocks if not any(
        any(p["text"] == b.text for p in s["paragraphs"]) for s in sections)]


def detect_captions(blocks: list[Block]) -> dict:
    figs, tabs = [], []
    for b in blocks:
        t = b.text.strip()
        mf, mt = CAP_FIG.match(t), CAP_TAB.match(t)
        if mf:
            figs.append({"label": f"Figure {mf.group(1)}", "caption": t,
                         "page": b.page, "bbox": [round(v, 2) for v in b.bbox]})
        elif mt:
            tabs.append({"label": f"Table {mt.group(1)}", "caption": t,
                         "page": b.page, "bbox": [round(v, 2) for v in b.bbox]})
    return {"figures": figs, "tables": tabs}


def detect_formulas(blocks: list[Block]) -> list[dict]:
    """公式：靠数学字体 / 符号密度 / 编号 三条线索，只取行内证据，不求还原语义。"""
    out = []
    for b in blocks:
        t = b.text.strip()
        if len(t) > 300 or len(t) < 3:
            continue
        math_font = any(k in (b.lines[0].font if b.lines else "").lower()
                        for k in ("cmmi", "cmsy", "symbol", "math", "stix"))
        symbols = sum(1 for ch in t if ch in "∑∫∂√±×÷≤≥≈≠≡∈∉⊂⊃∪∩∞αβγδεθλμπρσφχψωΔΩ")
        dense = symbols >= 3 or (symbols >= 1 and len(t) < 60)
        numbered = bool(EQ_NUM.search(t))
        if (math_font and len(t) < 200) or (dense and len(t) < 160) or numbered:
            m = EQ_NUM.search(t)
            out.append({"text": t, "equation_number": m.group(1) if m else None,
                        "page": b.page, "bbox": [round(v, 2) for v in b.bbox],
                        "signal": "font" if math_font else ("symbols" if dense else "number")})
    return out


def detect_references(blocks: list[Block]) -> tuple[list[dict], str | None]:
    start = None
    for i, b in enumerate(blocks):
        if re.match(r"^(references|bibliography)\b", b.text.strip(), re.I) and len(b.text) < 40:
            start = i
            break
    if start is None:
        return [], "no references heading found"

    entries: list[dict] = []
    buf: list[str] = []
    year = re.compile(r"\b(19|20)\d{2}[a-z]?\b")

    def flush(page: int) -> None:
        if not buf:
            return
        raw = re.sub(r"\s+", " ", " ".join(buf)).strip()
        if len(raw) >= 15:
            entries.append({"raw": raw, "page": page})
        buf.clear()

    for b in blocks[start + 1:]:
        t = b.text.strip()
        if not t:
            continue
        if re.match(r"^(appendix|supplementary|acknowledge?ment)", t, re.I) and len(t) < 40:
            break
        bracketed = re.match(r"^\[(\d+)\]\s*", t)
        numbered = re.match(r"^(\d+)\.\s+\S", t)
        starts_lower = t[:1].islower()
        # 每条参考文献独立成块（作者开头 + 年份）；续行一般小写开头
        looks_new = bool(bracketed or numbered) or (
            t[:1].isupper() and year.search(t) and not starts_lower)
        if looks_new and buf:
            flush(b.page)
        buf.append(t)
    flush(blocks[-1].page if blocks else 0)

    cleaned = []
    for n, e in enumerate(entries, 1):
        raw = e["raw"]
        m = re.match(r"^\[(\d+)\]\s*(.*)$", raw)
        num = m.group(1) if m else None
        body = m.group(2) if m else raw
        cleaned.append({"id": f"ref-{n:03d}", "number": num, "raw": body,
                        "year": (year.search(body).group(0) if year.search(body) else None),
                        "page": e["page"]})
    return cleaned, None
