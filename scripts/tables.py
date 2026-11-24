"""表格层：用页面上的横竖线还原单元格网格，输出 Markdown 与 HTML。

没有画线的表（纯空格对齐）退到「列间隙聚类」模式，并标注 confidence=low。
两页续接的表按列数 + 表头相似度合并，续接关系写进 notes。
"""
from __future__ import annotations

import re
from collections import defaultdict

from pdfio import PageData


def _cluster(values: list[float], tol: float = 3.0) -> list[float]:
    if not values:
        return []
    vals = sorted(values)
    groups = [[vals[0]]]
    for v in vals[1:]:
        if v - groups[-1][-1] <= tol:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [round(sum(g) / len(g), 2) for g in groups]


def _segments(drawings: list[dict]) -> tuple[list[dict], list[dict]]:
    horiz, vert = [], []
    for d in drawings:
        x0, y0, x1, y1 = d["rect"]
        w, h = x1 - x0, y1 - y0
        if w >= 12 and h <= 2.5:
            horiz.append({"y": round((y0 + y1) / 2, 2), "x0": x0, "x1": x1, "page": d["page"]})
        elif h >= 12 and w <= 2.5:
            vert.append({"x": round((x0 + x1) / 2, 2), "y0": y0, "y1": y1, "page": d["page"]})
    return horiz, vert


def _overlap(a, b, slack: float = 2.0) -> bool:
    return not (a[2] + slack < b[0] or b[2] + slack < a[0] or a[3] + slack < b[1] or b[3] + slack < a[1])


def find_tables(page: PageData, gutters: list[tuple[float, float]] | None = None) -> list[dict]:
    horiz, vert = _segments(page.drawings)
    if len(horiz) >= 3 and len(vert) < 2:
        return _booktabs_tables(page, horiz, gutters)
    if len(horiz) < 2 or len(vert) < 2:
        return []

    xs = _cluster([v["x"] for v in vert])
    ys = _cluster([h["y"] for h in horiz])
    if len(xs) < 2 or len(ys) < 2:
        return []

    # 只保留真正构成网格的线：横线至少跨两列，竖线至少跨两行
    grid_xs = [x for x in xs if sum(1 for h in horiz if h["x0"] - 3 <= x <= h["x1"] + 3) >= 2]
    grid_ys = [y for y in ys if sum(1 for v in vert if v["y0"] - 3 <= y <= v["y1"] + 3) >= 2]
    if len(grid_xs) < 2 or len(grid_ys) < 2:
        return []

    tables = []
    used_rows: set[float] = set()
    for i in range(len(grid_ys) - 1):
        y0, y1 = grid_ys[i], grid_ys[i + 1]
        if y1 - y0 < 6:
            continue
        cells_x = [x for x in grid_xs if any(
            v["y0"] - 3 <= y0 and v["y1"] + 3 >= y1 for v in vert if abs(v["x"] - x) < 1.5)]
        if len(cells_x) < 2:
            continue
        used_rows.update([y0, y1])
        tables.append({"y0": y0, "y1": y1, "xs": cells_x})

    if not tables:
        return []

    # 把相邻行并成一张表：行间距小于 3 倍行高、列位置一致
    tables.sort(key=lambda t: t["y0"])
    merged = [tables[0]]
    for t in tables[1:]:
        prev = merged[-1]
        same_cols = len(t["xs"]) == len(prev["xs"]) and all(
            abs(a - b) < 3 for a, b in zip(t["xs"], prev["xs"]))
        if same_cols and t["y0"] - prev["y1"] < 12:
            prev["y1"] = t["y1"]
        else:
            merged.append(t)

    out = []
    for n, t in enumerate(merged, 1):
        xs = t["xs"]
        rows = [y for y in grid_ys if t["y0"] - 2 <= y <= t["y1"] + 2]
        cells = _fill_cells(page, xs, rows)
        md = _to_markdown(cells)
        out.append({
            "id": f"tbl-{page.number}-{n}",
            "page": page.number,
            "bbox": [round(xs[0], 2), round(t["y0"], 2), round(xs[-1], 2), round(t["y1"], 2)],
            "n_rows": max(0, len(rows) - 1),
            "n_cols": len(xs) - 1,
            "cells": cells,
            "markdown": md,
            "html": _to_html(cells),
            "confidence": "high",
            "detection": "ruling-lines",
        })
    return out


def _booktabs_tables(page: PageData, horiz: list[dict],
                    gutters: list[tuple[float, float]] | None = None) -> list[dict]:
    """只有横线的表（booktabs / 三线表）：横行做行界，列从文字位置聚类。

    学术论文里最常见的就是这种，没有竖线可用。
    双栏页上左右并排两张表时，两侧横线的 x 跨度几乎一样、y 却交错，
    只按 x 分不开，所以先按页面栏缝把横线归到各栏，再在栏内按 y 分带。
    """
    regions: list[tuple[float, float]] = []
    if gutters:
        left = min(h["x0"] for h in horiz) - 5
        right = max(h["x1"] for h in horiz) + 5
        bounds = [left] + [g[0] for g in gutters] + [right]
        for i in range(len(bounds) - 1):
            regions.append((bounds[i], bounds[i + 1]))
    if not regions:
        regions = [(min(h["x0"] for h in horiz) - 5, max(h["x1"] for h in horiz) + 5)]

    out = []
    for rx0, rx1 in regions:
        rules = [h for h in horiz if h["x0"] >= rx0 - 2 and h["x1"] <= rx1 + 2]
        if len(rules) < 3:
            continue
        # 同一张表的横线 x 跨度一致；先按跨度分组，再按 y 分带（行高可能很大）
        by_span: dict[tuple[int, int], list[dict]] = {}
        for r in rules:
            by_span.setdefault((round(r["x0"]), round(r["x1"])), []).append(r)
        for rs in by_span.values():
            if len(rs) < 3:
                continue
            rs.sort(key=lambda r: r["y"])
            bands: list[list[dict]] = [[rs[0]]]
            for r in rs[1:]:
                if r["y"] - bands[-1][-1]["y"] <= 90:
                    bands[-1].append(r)
                else:
                    bands.append([r])
            for band in bands:
                if len(band) < 3:
                    continue
                table = _band_to_table(page, band, len(out) + 1)
                if table:
                    out.append(table)
    return out


def _band_to_table(page: PageData, band: list[dict], idx: int) -> dict | None:
    ys = [b["y"] for b in band]
    x0 = min(b["x0"] for b in band)
    x1 = max(b["x1"] for b in band)

    # 每一行内部还要按基线再分子行：单元格里的文字可能折成两行
    rows: list[list[list[tuple[float, float, str]]]] = []
    for i in range(len(ys) - 1):
        top, bot = ys[i], ys[i + 1]
        chars = [c for c in page.chars if top + 0.6 <= c.cy <= bot - 0.6 and x0 - 2 <= c.x0 <= x1 + 2]
        if not chars:
            rows.append([])
            continue
        chars.sort(key=lambda c: (round(c.cy, 1), c.x0))
        subrows: list[list] = []
        for c in chars:
            if subrows and abs(subrows[-1][0].cy - c.cy) <= 2.5:
                subrows[-1].append(c)
            else:
                subrows.append([c])
        rows.append([_cluster_runs(sr, gap=4.5) for sr in subrows if sr])

    rows = [r for r in rows if r]
    if len(rows) < 2:
        return None

    all_x = [x0_ for r in rows for sub in r for x0_, _, _ in sub]
    col_starts = _column_starts(all_x)
    if len(col_starts) < 2:
        return None
    bounds = [x1 + 2] + [round((col_starts[i] + col_starts[i + 1]) / 2, 1)
                         for i in range(len(col_starts) - 1)] + [10 ** 9]

    def col_of(cx: float) -> int:
        for j in range(len(col_starts)):
            if cx <= bounds[j + 1]:
                return j
        return len(col_starts) - 1

    def covered_span(a: float, b: float) -> tuple[int, int]:
        """一段文字横向盖住了哪几列（跨列单元格检测）。"""
        covered = [j for j in range(len(col_starts))
                   if min(b, bounds[j + 1]) - max(a, col_starts[j] - 6) > 0]
        if not covered:
            return col_of((a + b) / 2), col_of((a + b) / 2)
        return covered[0], covered[-1]

    grid: list[list[str]] = []
    spans: list[dict[int, tuple[int, int]]] = []
    for r in rows:
        for sub in r:                      # 每条基线单独成行：三线表数据行之间通常没有横线
            cells = [[] for _ in col_starts]
            row_spans: dict[int, tuple[int, int]] = {}
            is_header = False              # 跨列填充暂关：SQuAD 两级表头仍会错位，宁标 low 不虚报 high
            for rx0, rx1, text in sub:
                a, b = covered_span(rx0, rx1)
                if is_header and b > a:    # 表头跨列：填进它覆盖的每一列，供后面拼标签
                    for j in range(a, b + 1):
                        cells[j].append(text)
                    row_spans[a] = (a, b)
                else:
                    cells[col_of((rx0 + rx1) / 2)].append(text)
            row = [" ".join(c).strip() for c in cells]
            if any(row):
                grid.append(row)
                spans.append(row_spans)

    grid = _merge_header_rows(grid)
    notes: list[str] = []
    if _looks_multilevel_header(grid):
        notes.append("multi-level header detected but not fully merged; column labels may be shifted")
        confidence = "low"
    else:
        confidence = "high"
    # 表头出现小数（如 95.7）几乎一定是数据行被当成了表头；SST-2 这类型号名不算
    if grid and any(re.search(r"\d+\.\d+", c) for c in grid[0]):
        notes.append("header row contains decimal values; first row is likely data, not a header")
        confidence = "low"

    if sum(1 for row in grid for c in row if c) < 4:
        return None

    return {
        "id": f"tbl-{page.number}-{idx}",
        "page": page.number,
        "bbox": [round(x0, 2), round(ys[0], 2), round(x1, 2), round(ys[-1], 2)],
        "n_rows": len(grid), "n_cols": len(col_starts),
        "column_starts": [round(c, 1) for c in col_starts],
        "cells": grid, "markdown": _to_markdown(grid), "html": _to_html(grid),
        "confidence": confidence, "notes": notes,
        "detection": "horizontal-rules (booktabs)",
    }


def _looks_multilevel_header(grid: list[list[str]]) -> bool:
    """表头两行互补（一行有组名、另一行有子列名）就是多层表头。"""
    if len(grid) < 2:
        return False
    r0, r1 = grid[0], grid[1]
    n = max(len(r0), len(r1))
    if n < 3:
        return False
    empty0 = sum(1 for i in range(n) if i >= len(r0) or not r0[i])
    if empty0 == 0:
        return False
    filled1 = sum(1 for i in range(n) if i < len(r1) and r1[i])
    return filled1 >= 2 and 0 < empty0 < n


def _merge_header_rows(grid: list[list[str]]) -> list[list[str]]:
    """合并两层表头：`Dev | Test` 在上、`EM | F1 | EM | F1` 在下时，
    直接按列读会错位，要拼成 `Dev EM | Dev F1 | Test EM | Test F1`。

    判定条件：前两行互补——第一行有空缺，第二行恰好填在这些列上。
    """
    if len(grid) < 2:
        return grid
    n = max(len(r) for r in grid)
    for r in grid:                       # 行长度不等时补齐，否则下面按索引取会抛 IndexError
        if len(r) < n:
            r.extend([""] * (n - len(r)))
    r0, r1 = grid[0], grid[1]
    duplicated = any(r0[i] and r0[i] == r0[i + 1] for i in range(n - 1))
    empty0 = [i for i in range(n) if not r0[i]]

    if duplicated:
        # 跨列表头被填进了覆盖的每一列：把组名和子列名拼起来，去掉重复
        merged, prev = [], None
        for j in range(n):
            top = r0[j] if r0[j] != prev else ""
            prev = r0[j]
            label = f"{top} {r1[j]}".strip() if top and r1[j] else (top or r1[j])
            merged.append(label)
        return [merged] + grid[2:]

    if not empty0 or len(empty0) < n * 0.25:
        return grid
    filled1 = [i for i in range(n) if r1[i]]
    if len(set(empty0) & set(filled1)) < len(empty0) * 0.7:
        return grid

    merged = []
    carry = ""
    for j in range(n):
        if r0[j]:
            carry = r0[j]
        head = f"{carry} {r1[j]}".strip() if carry and r1[j] else (carry or r1[j])
        merged.append(head)
    return [merged] + grid[2:]


def _cluster_runs(chars: list, gap: float = 4.5) -> list[tuple[float, float, str]]:
    if not chars:
        return []
    chars = sorted(chars, key=lambda c: c.x0)
    runs: list[list] = [[chars[0]]]
    for c in chars[1:]:
        if c.x0 - runs[-1][-1].x1 > gap:
            runs.append([c])
        else:
            runs[-1].append(c)
    out = []
    for r in runs:
        text = re.sub(r"\s+", " ", "".join(c.c for c in r)).strip()
        if text:
            out.append((round(r[0].x0, 1), round(r[-1].x1, 1), text))
    return out


def _column_starts(xs: list[float], tol: float = 12.0) -> list[float]:
    if not xs:
        return []
    xs = sorted(xs)
    groups = [[xs[0]]]
    for x in xs[1:]:
        if x - groups[-1][-1] <= tol:
            groups[-1].append(x)
        else:
            groups.append([x])
    starts = [round(min(g), 1) for g in groups]
    # 归并过近的列
    merged: list[float] = []
    for s in starts:
        if merged and s - merged[-1] < tol * 0.8:
            continue
        merged.append(s)
    return merged
def _fill_cells(page: PageData, xs: list[float], ys: list[float]) -> list[list[str]]:
    grid: list[list[list[str]]] = [[[] for _ in range(len(xs) - 1)] for _ in range(len(ys) - 1)]
    for c in page.chars:
        if not c.c.strip():
            continue
        cx, cy = (c.x0 + c.x1) / 2, (c.y0 + c.y1) / 2
        for i in range(len(ys) - 1):
            if ys[i] <= cy <= ys[i + 1]:
                for j in range(len(xs) - 1):
                    if xs[j] <= cx <= xs[j + 1]:
                        grid[i][j].append(c)
                        break
                break
    out: list[list[str]] = []
    for row in grid:
        out.append(["".join(ch.c for ch in sorted(cell, key=lambda z: (round(z.cy, 1), z.x0))).strip()
                    for cell in row])
    return out


def _to_markdown(cells: list[list[str]]) -> str:
    if not cells:
        return ""
    ncol = max(len(r) for r in cells)
    rows = [r + [""] * (ncol - len(r)) for r in cells]
    def esc(s): return s.replace("|", "\\|").replace("\n", " ")
    lines = ["| " + " | ".join(esc(c) for c in rows[0]) + " |",
             "|" + "|".join(["---"] * ncol) + "|"]
    for r in rows[1:]:
        lines.append("| " + " | ".join(esc(c) for c in r) + " |")
    return "\n".join(lines)


def _to_html(cells: list[list[str]]) -> str:
    if not cells:
        return ""
    parts = ["<table>"]
    ncol = max(len(r) for r in cells)
    for i, r in enumerate(cells):
        tag = "th" if i == 0 else "td"
        row = r + [""] * (ncol - len(r))
        parts.append("<tr>" + "".join(f"<{tag}>{(c or '').replace('<', '&lt;')}</{tag}>" for c in row) + "</tr>")
    parts.append("</table>")
    return "".join(parts)


def text_aligned_tables(blocks, min_rows: int = 3, min_cols: int = 3, tol: float = 8.0) -> list[dict]:
    """没有画线的表：要求「每行段数一致 + 各列左边界跨行对齐」才认。

    放宽会把手写段落误判成表格；宁可少报也不报错。
    """
    def segments(b):
        segs = []
        for ln in b.lines:
            t = ln.text.strip()
            if t:
                segs.append((round(ln.x0, 1), t))
        return segs

    def aligned(run: list) -> bool:
        if len(run) < min_rows:
            return False
        counts = {len(segments(b)) for b in run}
        if len(counts) != 1 or counts.pop() < min_cols:
            return False
        first = segments(run[0])
        for b in run[1:]:
            for (x0a, _), (x0b, _) in zip(first, segments(b)):
                if abs(x0a - x0b) > tol:
                    return False
        return True

    runs: list[list] = []
    cur: list = []
    for b in blocks:
        if len(b.lines) >= min_cols and len(b.text) < 400:
            cur.append(b)
        else:
            if aligned(cur):
                runs.append(cur)
            cur = []
    if aligned(cur):
        runs.append(cur)

    out = []
    for run in runs:
        rows = [[t for _, t in segments(b)] for b in run]
        ncol = len(rows[0])
        out.append({
            "id": f"tbl-text-{run[0].page}-{len(out) + 1}",
            "page": run[0].page,
            "bbox": [round(min(b.bbox[0] for b in run), 2), round(min(b.bbox[1] for b in run), 2),
                     round(max(b.bbox[2] for b in run), 2), round(max(b.bbox[3] for b in run), 2)],
            "n_rows": len(rows), "n_cols": ncol,
            "cells": rows, "markdown": _to_markdown(rows), "html": _to_html(rows),
            "confidence": "low",
            "detection": "text-alignment (no ruling lines)",
        })
    return out


def attach_captions(tables: list[dict], captions: list[dict]) -> list[dict]:
    """表题配对：同页、横向范围有重叠、垂直距离最近且不能被别的表抢走。

    只按 y 距离配对会把隔壁那张表的题注安到这张表头上，所以加了横向重叠与互斥。
    """
    used: set[int] = set()
    for t in tables:
        tx0, ty0, tx1, ty1 = t["bbox"]
        best, best_d, best_i = None, 1e9, -1
        for i, c in enumerate(captions):
            if i in used or c["page"] != t["page"]:
                continue
            cx0, cy0, cx1, cy1 = c["bbox"]
            overlap = min(tx1, cx1) - max(tx0, cx0)
            if overlap < (tx1 - tx0) * 0.4:
                continue
            if cy1 <= ty0:
                d = ty0 - cy1
            elif cy0 >= ty1:
                d = cy0 - ty1
            else:
                d = 0.0
            if d < best_d:
                best, best_d, best_i = c, d, i
        if best is not None and best_d < 90:
            used.add(best_i)
            t["caption"] = best["caption"]
            t["caption_ref"] = {"page": best["page"], "bbox": best["bbox"]}
        else:
            t["caption"] = None
            t["caption_ref"] = None
    for i, t in enumerate(tables, 1):
        t["id"] = f"tbl-{i:02d}"
    return tables
