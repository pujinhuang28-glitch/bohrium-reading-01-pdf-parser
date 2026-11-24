"""版面层：行聚合 → 分栏 → 阅读顺序 → 段落合并 → 页眉页脚剔除。

难点集中在「排得对」：双栏论文如果按 x 排序会把左右栏拼成一句话；
按 y 排序又会串行。这里先找栏间空白（gutter），再把整宽行当分隔带（band），
带内先左栏后右栏。
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from pdfio import Char, PageData, Span

HYPHEN_END = re.compile(r"[A-Za-z]-$")
# 行尾断字要不要保留连字符：真正的复合词断开要留（left-to-|right、pre-|training），
# 排版断字才去掉（representa-|tion）。没有词典，只能用几条稳的规则。
COMPOUND_HEADS = {
    "pre", "post", "non", "multi", "self", "cross", "sub", "anti", "semi", "co",
    "over", "under", "well", "low", "high", "task", "fine", "off", "on", "in",
    "out", "up", "down", "left", "right", "to", "state", "end", "two", "three",
    "one", "zero", "few", "many", "large", "small", "long", "short", "real",
}


def join_hyphenated(left: str, right: str) -> str:
    """合并跨行断字。保留真正属于复合词的连字符。"""
    if not left.endswith("-"):
        return left + right
    token = re.split(r"\s+", left.strip())[-1]      # 只看断行处那个词，不看整段
    stem = token.rstrip("-")
    if stem.count("-") > 0:                 # left-to- 、state-of-the- 这类已有连字符
        return left + right
    if stem.lower() in COMPOUND_HEADS:
        return left + right
    if right[:1].isupper() or right[:1].isdigit():
        return left + right
    return left[:-1] + right
PAGE_NO = re.compile(r"^\s*(?:page\s*)?[\-–—]?\s*\d{1,4}\s*[\-–—]?\s*$", re.I)


@dataclass
class Line:
    text: str
    bbox: tuple[float, float, float, float]
    size: float
    bold: bool
    italic: bool
    font: str
    page: int
    spans: list[Span] = field(default_factory=list)
    chars: list[Char] = field(default_factory=list)
    column: int = 0
    full_width: bool = False

    @property
    def x0(self) -> float: return self.bbox[0]
    @property
    def x1(self) -> float: return self.bbox[2]
    @property
    def y0(self) -> float: return self.bbox[1]
    @property
    def y1(self) -> float: return self.bbox[3]
    @property
    def height(self) -> float: return max(1.0, self.y1 - self.y0)

    def as_dict(self) -> dict:
        return {"text": self.text, "bbox": [round(v, 2) for v in self.bbox],
                "page": self.page, "column": self.column, "full_width": self.full_width}


@dataclass
class Block:
    """一段按阅读顺序排好的文字，是后续章节/段落切分的最小单位。"""
    id: str
    page: int
    bbox: tuple[float, float, float, float]
    text: str
    size: float
    bold: bool
    italic: bool
    column: int
    kind: str = "body"          # body | header | footer
    lines: list[Line] = field(default_factory=list)


def compute_gutters(page: PageData, min_width: float = 9.0, max_row_ratio: float = 0.38) -> list[tuple[float, float]]:
    """找栏缝：某条竖缝上只有极少数文字行横跨，就当它是栏间空白。

    不能只用「完全没字」：首页的摘要/大图往往是整宽的，会把栏缝整条堵死。
    所以按「有多少行跨过这条缝」的比例判，而不是按有没有字。
    """
    chars = [c for c in page.chars if c.c.strip()]
    if len(chars) < 40:
        return []
    lo = min(c.x0 for c in chars)
    hi = max(c.x1 for c in chars)
    if hi - lo < page.width * 0.35:
        return []

    # 先按基线分行，再统计每条竖缝被多少行横跨
    rows: list[list] = []
    for c in sorted(chars, key=lambda z: (round(z.cy, 1), z.x0)):
        if rows:
            ref = rows[-1][-1]
            same_line = abs(ref.cy - c.cy) <= 2.5 and c.x0 - ref.x1 <= 8.0
            if same_line:
                rows[-1].append(c)
                continue
        rows.append([c])
    if len(rows) < 6:
        return []

    step = 1.0
    n = max(1, int((hi - lo) / step))
    crossings = [0] * n
    for r in rows:
        a = max(0, int((min(c.x0 for c in r) - lo) / step))
        b = min(n, int((max(c.x1 for c in r) - lo) / step) + 1)
        for i in range(a, max(a + 1, b)):
            if i < n:
                crossings[i] += 1

    limit = max(2, int(len(rows) * max_row_ratio))
    gutters, start = [], None
    for i, cnt in enumerate(crossings + [10 ** 9]):
        free = cnt <= limit
        if free and start is None:
            start = i
        elif not free and start is not None:
            gx0, gx1 = lo + start * step, lo + i * step
            if gx1 - gx0 >= min_width and gx0 > lo + 4 and gx1 < hi - 4:
                gutters.append((round(gx0, 2), round(gx1, 2)))
            start = None

    # 学术正文基本就是 1~2 栏：只认把页面从中间分开的那条缝，
    # 避免表格页里被大量短行切成 4、5 栏。
    body_w = hi - lo
    centred = [g for g in gutters if 0.33 <= ((g[0] + g[1]) / 2 - lo) / body_w <= 0.67]
    if not centred:
        return []
    best = max(centred, key=lambda g: g[1] - g[0])
    return [best]


def build_lines(page: PageData, y_tol: float = 2.5,
                gutters: list[tuple[float, float]] | None = None) -> list[Line]:
    """字符级重建行：按基线聚行 → 按栏缝/大间隙切分 → 逐字补空格。

    为什么要到字符级：
      * 双栏论文左右栏常常共用同一基线，按 y 聚行会把两栏并成一句；
      * 两端对齐的 PDF 里，词间空格有时不落成空格字符，只剩坐标差；
        只按坐标差补空格，而 span 边界处又补不出来，就会出现 "feature-basedandﬁne-tuning"。
    """
    if not page.chars:
        return _build_lines_from_spans(page, y_tol)

    if gutters is None:
        gutters = compute_gutters(page)
    gutter_cuts = [(a + b) / 2 for a, b in gutters]

    chars = sorted(page.chars, key=lambda c: (round(c.cy, 1), c.x0))
    rows: list[list[Char]] = []
    for ch in chars:
        placed = False
        for r in rows[-6:]:
            if abs(r[0].cy - ch.cy) <= max(y_tol * 0.4, 0.35 * ch.size):
                r.append(ch)
                placed = True
                break
        if not placed:
            rows.append([ch])

    gaps = []
    for r in rows:
        r.sort(key=lambda c: c.x0)
        for i in range(1, len(r)):
            g = r[i].x0 - r[i - 1].x1
            if -1 < g < 20:
                gaps.append(g)
    import statistics
    word_gap = statistics.median(gaps) if gaps else 2.0
    col_gap = max(6.0, word_gap * 2.2)
    space_gap = max(0.9, word_gap * 1.5)

    lines: list[Line] = []
    for r in rows:
        r.sort(key=lambda c: c.x0)
        cuts = sorted(set(gutter_cuts))
        segments: list[list[Char]] = [[r[0]]]
        for c in r[1:]:
            prev = segments[-1][-1]
            crossed_gutter = any(prev.x1 <= cut <= c.x0 or prev.x0 <= cut <= c.x1 for cut in cuts)
            if crossed_gutter or c.x0 - prev.x1 > col_gap:
                segments.append([c])
            else:
                segments[-1].append(c)
        for seg in segments:
            text = ""
            for i, c in enumerate(seg):
                if c.c == " ":
                    if not text.endswith(" ") and text:
                        text += " "
                    continue
                if i and not text.endswith(" "):
                    g = c.x0 - seg[i - 1].x1
                    if g > space_gap:
                        text += " "
                text += c.c
            text = re.sub(r"\s+", " ", text).strip()
            if not text:
                continue
            bbox = (min(c.x0 for c in seg), min(c.y0 for c in seg),
                    max(c.x1 for c in seg), max(c.y1 for c in seg))
            sizes = Counter(round(c.size, 1) for c in seg)
            lines.append(Line(text=text, bbox=bbox,
                              size=sizes.most_common(1)[0][0],
                              bold=sum(c.bold for c in seg) * 2 > len(seg),
                              italic=sum(c.italic for c in seg) * 2 > len(seg),
                              font=seg[0].font, page=page.number, chars=seg))
    lines.sort(key=lambda l: (l.y0, l.x0))
    return lines


def _build_lines_from_spans(page: PageData, y_tol: float) -> list[Line]:
    """没有字符坐标时的退路（pdftotext / 纯 span 后端）。"""
    buckets: list[list[Span]] = []
    for sp in sorted(page.spans, key=lambda s: (round(s.bbox[1], 1), s.bbox[0])):
        placed = False
        for b in buckets:
            ref = b[0]
            if abs(((ref.bbox[1] + ref.bbox[3]) / 2) - ((sp.bbox[1] + sp.bbox[3]) / 2)) <= y_tol:
                b.append(sp)
                placed = True
                break
        if not placed:
            buckets.append([sp])

    lines: list[Line] = []
    for b in buckets:
        b.sort(key=lambda s: s.bbox[0])
        text = ""
        for i, sp in enumerate(b):
            if i and sp.bbox[0] - b[i - 1].bbox[2] > sp.size * 0.45 and not text.endswith(" "):
                text += " "
            text += sp.text
        if not text.strip():
            continue
        bbox = (min(s.bbox[0] for s in b), min(s.bbox[1] for s in b),
                max(s.bbox[2] for s in b), max(s.bbox[3] for s in b))
        sizes = Counter(round(s.size, 1) for s in b)
        lines.append(Line(text=re.sub(r"\s+", " ", text).strip(), bbox=bbox,
                          size=sizes.most_common(1)[0][0],
                          bold=any(s.bold for s in b),
                          italic=all(s.italic for s in b) and len(b) > 0,
                          font=b[0].font, page=page.number, spans=b))
    lines.sort(key=lambda l: (l.y0, l.x0))
    return lines


def find_gutters(lines: list[Line], page_width: float, min_gap_ratio: float = 0.035) -> list[tuple[float, float]]:
    """用「没有任何行横跨的 x 区间」找栏间空白。"""
    if not lines:
        return []
    body = [l for l in lines if not l.full_width]
    if not body:
        return []
    lo = min(l.x0 for l in body)
    hi = max(l.x1 for l in body)
    step = 2.0
    n = max(1, int((hi - lo) / step))
    covered = [False] * n
    for l in body:
        a = max(0, int((l.x0 - lo) / step))
        b = min(n, int((l.x1 - lo) / step) + 1)
        for i in range(a, b):
            covered[i] = True
    min_gap = page_width * min_gap_ratio
    gutters, run_start = [], None
    for i, c in enumerate(covered):
        if not c and run_start is None:
            run_start = i
        elif c and run_start is not None:
            x0, x1 = lo + run_start * step, lo + i * step
            if x1 - x0 >= min_gap:
                gutters.append((round(x0, 2), round(x1, 2)))
            run_start = None
    if run_start is not None:
        x0, x1 = lo + run_start * step, lo + n * step
        if x1 - x0 >= min_gap:
            gutters.append((round(x0, 2), round(x1, 2)))
    # 只保留真正把页面切成若干栏的竖缝：排除页边空白
    return [(a, b) for a, b in gutters if a > lo + 5 and b < hi - 5]


def assign_columns(lines: list[Line], page_width: float,
                   gutters: list[tuple[float, float]] | None = None,
                   full_width_ratio: float = 0.72) -> list[float]:
    """给每行打栏号；返回栏边界。整宽行标 full_width。"""
    body_x0 = min((l.x0 for l in lines), default=0.0)
    body_x1 = max((l.x1 for l in lines), default=page_width)
    body_w = max(1.0, body_x1 - body_x0)

    for l in lines:
        l.full_width = (l.x1 - l.x0) >= body_w * full_width_ratio

    if gutters is None:
        gutters = find_gutters(lines, page_width)
    cuts = [a for a, _ in gutters]
    for l in lines:
        if l.full_width:
            l.column = 0
            continue
        cx = (l.x0 + l.x1) / 2
        l.column = sum(1 for c in cuts if cx > c)
    return cuts


def mark_running_heads(pages_lines: list[list[Line]], page_heights: list[float],
                       margin_ratio: float = 0.075) -> tuple[set[str], set[str]]:
    """页眉页脚：落在外边距内 + （跨页重复 或 形如页码）。"""
    candidates = defaultdict(list)
    for lines, h in zip(pages_lines, page_heights):
        for l in lines:
            if l.y1 < h * margin_ratio or l.y0 > h * (1 - margin_ratio):
                candidates[normalize_furniture(l.text)].append((l.page, l.text))
    repeated = {k for k, v in candidates.items() if len({p for p, _ in v}) >= 3}
    footers: set[str] = set()
    for k, v in candidates.items():
        if k in repeated or all(PAGE_NO.match(t) for _, t in v):
            for p, t in v:
                footers.add(f"{p}:{t}")
    return repeated, footers


def normalize_furniture(text: str) -> str:
    t = re.sub(r"\d+", "#", text.strip().lower())
    return re.sub(r"\s+", " ", t)


def is_furniture(line: Line, page_height: float, repeated: set[str],
                 footers: set[str], margin_ratio: float = 0.075) -> str | None:
    in_margin = line.y1 < page_height * margin_ratio or line.y0 > page_height * (1 - margin_ratio)
    if not in_margin:
        return None
    if normalize_furniture(line.text) in repeated:
        return "header_or_footer"
    if f"{line.page}:{line.text.strip()}" in footers or PAGE_NO.match(line.text):
        return "page_number"
    if line.size < 9.5 and len(line.text) < 90:
        return "margin_text"
    return None


def order_lines(lines: list[Line]) -> list[Line]:
    """带内先左栏后右栏；整宽行按 y 插入。"""
    if not lines:
        return []
    bands: list[list[Line]] = [[]]
    for l in sorted(lines, key=lambda x: (x.y0, x.x0)):
        if l.full_width:
            bands.append([l])
            bands.append([])
        else:
            bands[-1].append(l)
    ordered: list[Line] = []
    for band in bands:
        if len(band) == 1 and band[0].full_width:
            ordered.append(band[0])
            continue
        for col in sorted({l.column for l in band}):
            ordered.extend(sorted((l for l in band if l.column == col), key=lambda x: (x.y0, x.x0)))
    return ordered


def merge_paragraphs(lines: list[Line], gap_ratio: float = 0.62) -> list[Block]:
    """同栏相邻行合成段；行尾连字符接回。"""
    blocks: list[Block] = []
    cur: list[Line] = []

    def flush() -> None:
        if not cur:
            return
        text = ""
        for i, l in enumerate(cur):
            if i and HYPHEN_END.search(text) and l.text[:1].islower():
                text = join_hyphenated(text, l.text)
            else:
                if i:
                    text += " "
                text += l.text
        bbox = (min(l.x0 for l in cur), min(l.y0 for l in cur),
                max(l.x1 for l in cur), max(l.y1 for l in cur))
        sizes = Counter(round(l.size, 1) for l in cur)
        blocks.append(Block(
            id=f"p{cur[0].page}-b{len(blocks) + 1}",
            page=cur[0].page, bbox=bbox, text=re.sub(r"\s+", " ", text).strip(),
            size=sizes.most_common(1)[0][0],
            bold=sum(l.bold for l in cur) * 2 > len(cur),
            italic=all(l.italic for l in cur),
            column=cur[0].column, lines=list(cur),
        ))
        cur.clear()

    for l in lines:
        if not cur:
            cur.append(l)
            continue
        prev = cur[-1]
        same_col = prev.column == l.column
        gap = l.y0 - prev.y1
        size_same = abs(l.size - prev.size) < 0.8
        if (same_col and size_same and gap < prev.height * gap_ratio
                and not l.full_width and not prev.full_width):
            cur.append(l)
        else:
            flush()
            cur.append(l)
    flush()
    for b in blocks:
        b.lines = list(b.lines)
    return blocks


def extract_layout(pages: list[PageData]) -> dict:
    """整篇：逐页分行、分栏、去页眉页脚，再给出全局阅读顺序。"""
    pages_lines: list[list[Line]] = []
    page_meta = []
    for p in pages:
        gutters = compute_gutters(p)
        lines = build_lines(p, gutters=gutters)
        cuts = assign_columns(lines, p.width, gutters=gutters)
        pages_lines.append(lines)
        page_meta.append({"page": p.number, "lines": len(lines), "gutters": cuts,
                          "columns": len(cuts) + 1, "text_layer": p.has_text_layer,
                          "rotated_chars": len(p.rotated)})

    repeated, footers = mark_running_heads(pages_lines, [p.height for p in pages])

    ordered_blocks: list[Block] = []
    furniture_blocks: list[Block] = []
    per_page = []
    for p, lines in zip(pages, pages_lines):
        kept, dropped = [], []
        for l in lines:
            why = is_furniture(l, p.height, repeated, footers)
            (dropped if why else kept).append(l)
        for l in dropped:
            b = Block(id=f"p{l.page}-f{len(furniture_blocks) + 1}", page=l.page, bbox=l.bbox,
                      text=l.text, size=l.size, bold=l.bold, italic=l.italic,
                      column=l.column, kind="footer", lines=[l])
            furniture_blocks.append(b)
        blocks = merge_paragraphs(order_lines(kept))
        ordered_blocks.extend(blocks)
        per_page.append({"page": p.number, "blocks": len(blocks),
                         "dropped_margin_lines": len(dropped)})

    return {
        "page_meta": page_meta,
        "per_page": per_page,
        "blocks": ordered_blocks,
        "furniture": furniture_blocks,
        "repeated_furniture": sorted(repeated)[:20],
    }
