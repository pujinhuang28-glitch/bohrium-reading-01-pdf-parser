#!/usr/bin/env python3
"""文献 PDF → 结构化解析结果。

入口：
    python parse_pdf.py --pdf /input/paper.pdf --out /app/output
    python parse_pdf.py --pdf x.pdf --case-file /input/case.json --out ./out --render

产出（--out 目录下）：
    result.json    结构化结果（含页级证据、状态、不确定性）
    parsed.md      人读版本：正文按阅读顺序 + 章节树 + 表格
    tables/tbl-XX.md  每张表的独立 Markdown
    selfcheck.json 自检结果
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import layout as lay
import structure as st
import tables as tb
from pdfio import read_pdf, render_page_png, probe_backends

VERSION = "1.0.0"


def load_case(case_file: str | None) -> dict:
    if not case_file:
        return {}
    p = Path(case_file)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def pair_figures(pages, caption_blocks: list[dict], blocks: list) -> list[dict]:
    """图片块与题注配对；没有位图时（矢量图）用题注锚定区域，并注明推断方式。"""
    figs = []
    used_captions: set[str] = set()
    for p in pages:
        for im in p.images:
            bx0, by0, bx1, by1 = im["bbox"]
            if (bx1 - bx0) < 60 or (by1 - by0) < 60:
                continue
            best, best_d = None, 1e9
            for c in caption_blocks:
                if c["page"] != p.number:
                    continue
                cy = c["bbox"][1]
                d = min(abs(cy - by1), abs(by0 - c["bbox"][3]))
                if d < best_d:
                    best, best_d = c, d
            cap = best if best and best_d < 140 else None
            if cap:
                used_captions.add(cap["caption"])
            figs.append({
                "id": f"fig-img-{p.number}-{len(figs) + 1}",
                "page": p.number,
                "bbox": [round(v, 2) for v in im["bbox"]],
                "caption": cap["caption"] if cap else None,
                "caption_ref": {"page": cap["page"], "bbox": cap["bbox"]} if cap else None,
                "graphic": "raster",
                "located_by": "image-block",
            })

    # 矢量图：位图检测不到，只能靠 Figure N 题注锚定版面区域
    for c in caption_blocks:
        if c["caption"] in used_captions:
            continue
        page_blocks = [b for b in blocks if b.page == c["page"]]
        above = [b for b in page_blocks if b.bbox[3] < c["bbox"][1] - 2]
        top = max((b.bbox[3] for b in above), default=c["bbox"][1] - 260)
        figs.append({
            "id": f"fig-cap-{c['page']}-{len(figs) + 1}",
            "page": c["page"],
            "bbox": [round(c["bbox"][0], 2), round(top, 2), round(c["bbox"][2], 2), round(c["bbox"][3], 2)],
            "caption": c["caption"],
            "caption_ref": {"page": c["page"], "bbox": c["bbox"]},
            "graphic": "vector-or-unknown",
            "located_by": "caption-anchor (region estimated from nearest text above)",
        })

    figs.sort(key=lambda f: (f["page"], f["bbox"][1]))
    for n, f in enumerate(figs, 1):
        f["id"] = f"fig-{n:02d}"
    return figs


def build_markdown(doc: dict) -> str:
    lines = [f"# {doc['document'].get('title') or '(title not detected)'}", ""]
    if doc["document"].get("authors"):
        lines += [f"**Authors:** {doc['document']['authors']}", ""]
    if doc["document"].get("abstract"):
        lines += ["## Abstract", "", doc["document"]["abstract"], ""]
    for s in doc["sections"]:
        lines += [f"{'#' * min(6, s['level'] + 1)} {s['title']}", ""]
        for p in s["paragraphs"]:
            lines += [p["text"], "", f"<!-- page {p['page']} bbox {p['bbox']} -->", ""]
    if doc["tables"]:
        lines += ["## Tables", ""]
        for t in doc["tables"]:
            cap = t.get("caption") or t["id"]
            lines += [f"### {cap}", "", f"*page {t['page']} · {t['n_rows']}x{t['n_cols']} · confidence={t['confidence']}*",
                      "", t["markdown"], ""]
    if doc["references"]:
        lines += ["## References", ""]
        for r in doc["references"]:
            lines.append(f"- {r['raw']}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--out", default="./output")
    ap.add_argument("--case-file", default=None)
    ap.add_argument("--render", action="store_true", help="把无文字层页面渲染成 PNG")
    ap.add_argument("--dpi", type=int, default=150)
    args = ap.parse_args()

    started = time.time()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "tables").mkdir(exist_ok=True)

    case = load_case(args.case_file)
    case_id = case.get("case_id") or case.get("id") or Path(args.pdf).stem
    pdf_path = Path(args.pdf)
    uncertainty: list[dict] = []
    changes: list[dict] = []

    try:
        pages, meta = read_pdf(pdf_path)
    except Exception as e:                                   # noqa: BLE001
        result = {
            "case_id": case_id, "status": "failed",
            "input": {"path": str(pdf_path), "sha256": None},
            "error": {"type": type(e).__name__, "message": str(e)},
            "uncertainty": [{"item": "pdf_read", "reason": str(e), "impact": "无法解析"}],
            "changes": [], "tool": {"name": "pdf-structure-parser", "version": VERSION},
        }
        (out_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"status": "failed", "case_id": case_id, "out": str(out_dir),
                          "error": result["error"]}, ensure_ascii=False))
        return 2

    if meta.get("degraded"):
        uncertainty.append({"item": "layout", "reason": f"后端降级为 {meta['backend']}，无坐标信息",
                            "impact": "分栏、表格、页码定位不可靠"})

    scanned = [p.number for p in pages if not p.has_text_layer and p.images]
    if scanned:
        uncertainty.append({"item": "text_layer", "reason": f"第 {scanned[:8]} 页几乎无文字层，疑似扫描页",
                            "impact": "这些页的正文需要 OCR，未 OCR 的部分不作猜测"})
        if args.render:
            for n in scanned[:12]:
                png = out_dir / "pages" / f"page-{n:03d}.png"
                if render_page_png(pdf_path, n, png, dpi=args.dpi):
                    changes.append({"action": "render_page", "page": n, "path": str(png.relative_to(out_dir))})

    lay_data = lay.extract_layout(pages)
    blocks = lay_data["blocks"]
    body = st.body_size(blocks)

    title, title_basis = st.detect_title(blocks, len(pages))
    abstract, abstract_ref = st.detect_abstract(blocks, body)

    ruled = []
    for p, pm in zip(pages, lay_data["page_meta"]):
        gs = []
        if pm["gutters"]:
            gs = [(c, c) for c in pm["gutters"]]
        ruled.extend(tb.find_tables(p, gutters=gs or None))
    tboxes = [(t["page"], tuple(t["bbox"])) for t in ruled]

    sections, orphans = st.detect_sections(blocks, body, tboxes)
    caps = st.detect_captions(blocks)
    figures = pair_figures(pages, caps["figures"], blocks)

    tables = ruled or tb.text_aligned_tables(blocks)
    if not ruled:
        uncertainty.append({"item": "tables", "reason": "未发现表格线，改用文本对齐推断",
                            "impact": "行列表格结构置信度低"})
    tables = tb.attach_captions(tables, caps["tables"])
    for t in tables:
        (out_dir / "tables" / f"{t['id']}.md").write_text(t["markdown"] + "\n", encoding="utf-8")

    formulas = st.detect_formulas(blocks)
    references, ref_note = st.detect_references(blocks)
    if ref_note:
        uncertainty.append({"item": "references", "reason": ref_note, "impact": "参考文献列表为空"})
    if formulas:
        uncertainty.append({"item": "formulas", "reason": "公式按版面元素定位，未还原 LaTeX 语义",
                            "impact": "符号与上下标以原文为准，可复核但不可直接计算"})

    coverage = {
        "pages": len(pages),
        "pages_with_text_layer": sum(1 for p in pages if p.has_text_layer),
        "blocks": len(blocks),
        "chars": sum(p.char_count for p in pages),
        "sections": len(sections),
        "tables": len(tables),
        "figures": len(figures),
        "formulas": len(formulas),
        "references": len(references),
        "running_headers_footers_removed": len(lay_data["furniture"]),
    }
    if not sections:
        uncertainty.append({"item": "sections", "reason": "未识别出章节标题",
                            "impact": "层级信息缺失，正文仍按阅读顺序保留"})
    if not abstract:
        uncertainty.append({"item": "abstract", "reason": "未定位到摘要段", "impact": "摘要字段为空"})

    status = "success"
    if uncertainty and any(u["item"] in ("text_layer", "layout") for u in uncertainty):
        status = "partial"

    result = {
        "case_id": case_id,
        "status": status,
        "input": {
            "path": str(pdf_path),
            "filename": pdf_path.name,
            "sha256": hashlib.sha256(pdf_path.read_bytes()).hexdigest()[:32],
            "pages": len(pages),
            "bytes": pdf_path.stat().st_size,
            "backend": meta.get("backend"),
            "backend_chain": meta.get("backend_chain"),
            "backends_available": meta.get("probe"),
        },
        "document": {
            "title": title,
            "title_basis": title_basis,
            "authors": next((b.text for b in blocks[:12] if 0 < len(b.text) < 200 and "@" in b.text), ""),
            "abstract": abstract,
            "abstract_ref": abstract_ref,
            "body_font_size": body,
            "language_hint": "en" if sum(1 for b in blocks[:40] if b.text.isascii()) > 30 else "mixed",
        },
        "sections": sections,
        "tables": tables,
        "figures": figures,
        "formulas": formulas,
        "references": references,
        "page_layout": lay_data["page_meta"],
        "running_furniture": [{"text": f.text, "page": f.page, "bbox": [round(v, 2) for v in f.bbox]}
                              for f in lay_data["furniture"][:40]],
        "coverage": coverage,
        "uncertainty": uncertainty,
        "changes": changes,
        "tool": {"name": "pdf-structure-parser", "version": VERSION,
                 "elapsed_s": round(time.time() - started, 2)},
    }

    (out_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "parsed.md").write_text(build_markdown(result), encoding="utf-8")

    print(json.dumps({"status": status, "case_id": case_id, "out": str(out_dir),
                      "coverage": coverage, "elapsed_s": result["tool"]["elapsed_s"]},
                     ensure_ascii=False))
    return 0 if status == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
