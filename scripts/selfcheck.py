#!/usr/bin/env python3
"""解析结果自检：不判断内容对错，只保证「结构完整 + 证据齐全 + 状态自洽」。

    python selfcheck.py --result /app/output/result.json [--pdf paper.pdf]

退出码 0 表示通过；有问题打印清单并返回 1。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

STATUSES = {"success", "partial", "failed"}


def check(result: dict, pdf_pages: int | None = None) -> list[str]:
    problems: list[str] = []

    if not result.get("case_id"):
        problems.append("case_id 为空：提交结果必须能指回输入案例")
    status = result.get("status")
    if status not in STATUSES:
        problems.append(f"status={status!r} 不合法，应为 {sorted(STATUSES)}")

    pages = pdf_pages or (result.get("input", {}) or {}).get("pages") or 0

    def bad_bbox(bb) -> bool:
        if not isinstance(bb, (list, tuple)) or len(bb) != 4:
            return True
        x0, y0, x1, y1 = bb
        if any(v is None for v in (x0, y0, x1, y1)):
            return True
        return x1 < x0 or y1 < y0

    def page_ok(p) -> bool:
        return isinstance(p, int) and p >= 1 and (pages == 0 or p <= pages)

    for s in result.get("sections", []) or []:
        if not page_ok(s.get("page")):
            problems.append(f"section {s.get('id')} 页码缺失或越界：{s.get('page')}")
        if bad_bbox(s.get("bbox")):
            problems.append(f"section {s.get('id')} bbox 非法")
        for p in s.get("paragraphs", []) or []:
            if not (p.get("text") or "").strip():
                problems.append(f"段落 {p.get('id')} 正文为空")
            if not page_ok(p.get("page")):
                problems.append(f"段落 {p.get('id')} 页码缺失或越界")
            if bad_bbox(p.get("bbox")):
                problems.append(f"段落 {p.get('id')} bbox 非法")

    for t in result.get("tables", []) or []:
        if not page_ok(t.get("page")):
            problems.append(f"表格 {t.get('id')} 页码缺失或越界")
        if bad_bbox(t.get("bbox")):
            problems.append(f"表格 {t.get('id')} bbox 非法")
        rows = t.get("cells") or []
        if not rows:
            problems.append(f"表格 {t.get('id')} 没有单元格")
        elif len(rows) != t.get("n_rows"):
            problems.append(f"表格 {t.get('id')} n_rows={t.get('n_rows')} 与 cells 行数 {len(rows)} 不符")
        if t.get("confidence") not in ("high", "low"):
            problems.append(f"表格 {t.get('id')} confidence 应为 high/low")

    for f in result.get("figures", []) or []:
        if not page_ok(f.get("page")):
            problems.append(f"图 {f.get('id')} 页码缺失或越界")
        if bad_bbox(f.get("bbox")):
            problems.append(f"图 {f.get('id')} bbox 非法")

    for r in result.get("references", []) or []:
        if not (r.get("raw") or "").strip():
            problems.append(f"参考文献 {r.get('id')} 为空")

    unc = result.get("uncertainty") or []
    items = {u.get("item") for u in unc}
    if items & {"text_layer", "layout"} and status == "success":
        problems.append("存在扫描页/后端降级，但 status 仍是 success，应标 partial")
    if status == "partial" and not unc:
        problems.append("status=partial 但 uncertainty 为空，需说明哪里不确定")
    for u in unc:
        if not (u.get("reason") or "").strip():
            problems.append(f"uncertainty[{u.get('item')}] 缺少 reason")

    cov = result.get("coverage") or {}
    if cov:
        for key in ("pages", "sections", "tables"):
            if key not in cov:
                problems.append(f"coverage 缺字段 {key}")
        if cov.get("sections") == 0:
            problems.append("coverage.sections=0：未识别出任何章节，检查标题判定")

    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--result", required=True)
    ap.add_argument("--pdf", default=None, help="可选：传原始 PDF，用来核对页数")
    args = ap.parse_args()

    result = json.loads(Path(args.result).read_text(encoding="utf-8"))
    pages = None
    if args.pdf:
        try:
            import pymupdf
            pages = pymupdf.open(args.pdf).page_count
        except Exception:
            pages = None

    problems = check(result, pages)
    print(json.dumps({"checked": args.result, "problems": problems, "ok": not problems},
                     ensure_ascii=False, indent=2))
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
