"""PDF 读取后端：优先 PyMuPDF，退化到 pdfplumber / pdftotext / pypdf。

评测容器只保证有 pdf-text-tool / pdf-render-tool / ocr-tool 这类工具，
不保证有某个 Python 库，所以这里先探测再选路，并把实际用的后端记进结果。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Char:
    c: str
    bbox: tuple[float, float, float, float]
    size: float
    font: str
    bold: bool
    italic: bool

    @property
    def x0(self) -> float: return self.bbox[0]
    @property
    def x1(self) -> float: return self.bbox[2]
    @property
    def y0(self) -> float: return self.bbox[1]
    @property
    def y1(self) -> float: return self.bbox[3]
    @property
    def cy(self) -> float: return (self.bbox[1] + self.bbox[3]) / 2


@dataclass
class Span:
    text: str
    bbox: tuple[float, float, float, float]
    size: float
    bold: bool
    italic: bool
    font: str
    page: int
    line_key: str = ""

    def as_dict(self) -> dict:
        return {"text": self.text, "bbox": [round(v, 2) for v in self.bbox],
                "size": round(self.size, 2), "bold": self.bold, "font": self.font, "page": self.page}


@dataclass
class PageData:
    number: int
    width: float
    height: float
    spans: list[Span] = field(default_factory=list)
    chars: list[Char] = field(default_factory=list)
    rotated: list[str] = field(default_factory=list)
    images: list[dict] = field(default_factory=list)
    drawings: list[dict] = field(default_factory=list)
    char_count: int = 0

    @property
    def has_text_layer(self) -> bool:
        # 扫描件常见：有图、几乎没有字
        return self.char_count >= 40

    def as_dict(self) -> dict:
        return {"page": self.number, "width": round(self.width, 2), "height": round(self.height, 2),
                "chars": self.char_count, "spans": len(self.spans),
                "images": len(self.images), "drawings": len(self.drawings),
                "text_layer": self.has_text_layer}


def _bold_from_flags(flags: int, font: str) -> bool:
    if flags & 16:                      # PyMuPDF bold flag
        return True
    name = (font or "").lower()
    return any(k in name for k in ("bold", "black", "heavy", "semibold", "demi"))


def _italic_from_flags(flags: int, font: str) -> bool:
    if flags & 2:
        return True
    name = (font or "").lower()
    return any(k in name for k in ("italic", "oblique"))


def probe_backends() -> dict:
    """报告本机可用的读取后端，写进 result.json 的 input 段。"""
    found = {}
    for mod in ("pymupdf", "fitz", "pdfplumber", "pypdf"):
        try:
            __import__(mod)
            found[mod] = True
        except Exception:
            found[mod] = False
    for cli in ("pdftotext", "pdftoppm", "tesseract"):
        found[cli] = bool(shutil.which(cli))
    return found


def _read_pymupdf(path: Path) -> tuple[list[PageData], dict]:
    import pymupdf

    doc = pymupdf.open(path)
    pages: list[PageData] = []
    for i, page in enumerate(doc):
        pd = PageData(number=i + 1, width=page.rect.width, height=page.rect.height)
        raw = page.get_text("rawdict")
        for b in raw.get("blocks", []):
            if b.get("type") != 0:
                bbox = tuple(b.get("bbox", (0, 0, 0, 0)))
                pd.images.append({"bbox": [round(v, 2) for v in bbox], "page": i + 1})
                continue
            for ln in b.get("lines", []):
                direction = ln.get("dir", (1.0, 0.0))
                if abs(direction[1]) > 0.1:          # 竖排/旋转文字：arXiv 水印一类，不当正文
                    pd.rotated.extend(
                        ch.get("c", "") for sp in ln.get("spans", []) for ch in sp.get("chars", []))
                    continue
                for sp in ln.get("spans", []):
                    flags = int(sp.get("flags", 0))
                    font = sp.get("font", "")
                    size = float(sp.get("size", 0))
                    txt = ""
                    for ch in sp.get("chars", []):
                        c = ch.get("c", "")
                        bbox = tuple(ch.get("bbox", (0, 0, 0, 0)))
                        txt += c
                        if c:
                            pd.chars.append(Char(
                                c=c, bbox=bbox, size=size, font=font,
                                bold=_bold_from_flags(flags, font),
                                italic=_italic_from_flags(flags, font)))
                    if txt.strip():
                        pd.spans.append(Span(
                            text=txt, bbox=tuple(sp.get("bbox", (0, 0, 0, 0))),
                            size=size, bold=_bold_from_flags(flags, font),
                            italic=_italic_from_flags(flags, font), font=font,
                            page=i + 1, line_key=f"{i+1}:{round(sp['bbox'][1],1)}",
                        ))
        try:
            for info in page.get_image_info():
                bb = tuple(info.get("bbox", (0, 0, 0, 0)))
                if not any(abs(bb[0] - im["bbox"][0]) < 1 and abs(bb[1] - im["bbox"][1]) < 1
                           for im in pd.images):
                    pd.images.append({"bbox": [round(v, 2) for v in bb], "page": i + 1})
        except Exception:
            pass
        try:
            for dr in page.get_drawings():
                pd.drawings.append({
                    "rect": [round(v, 2) for v in dr["rect"]],
                    "type": dr.get("type", ""),
                    "width": dr.get("width") or 0,
                    "page": i + 1,
                })
        except Exception:
            pass
        pd.char_count = sum(len(s.text) for s in pd.spans)
        pages.append(pd)
    meta = {"backend": "pymupdf", "pages": len(pages),
            "metadata": {k: v for k, v in (doc.metadata or {}).items() if v}}
    return pages, meta


def _read_pdfplumber(path: Path) -> tuple[list[PageData], dict]:
    import pdfplumber

    pages: list[PageData] = []
    with pdfplumber.open(str(path)) as pdf:
        for i, page in enumerate(pdf.pages):
            pd = PageData(number=i + 1, width=page.width, height=page.height)
            for ch in page.chars:
                bbox = (ch["x0"], ch["top"], ch["x1"], ch["bottom"])
                bold = "bold" in (ch.get("fontname", "") or "").lower()
                italic = "italic" in (ch.get("fontname", "") or "").lower()
                if ch.get("text", "").strip():
                    pd.chars.append(Char(c=ch.get("text", ""), bbox=bbox,
                                         size=float(ch.get("size", 0)),
                                         font=ch.get("fontname", ""), bold=bold, italic=italic))
                pd.spans.append(Span(
                    text=ch.get("text", ""),
                    bbox=bbox,
                    size=float(ch.get("size", 0)),
                    bold=bold, italic=italic,
                    font=ch.get("fontname", ""),
                    page=i + 1,
                    line_key=f"{i+1}:{round(ch['top'],1)}",
                ))
            for im in page.images:
                pd.images.append({"bbox": [round(im["x0"], 2), round(im["top"], 2),
                                           round(im["x1"], 2), round(im["bottom"], 2)], "page": i + 1})
            for ln in page.lines + page.rects:
                pd.drawings.append({"rect": [round(ln["x0"], 2), round(ln["top"], 2),
                                             round(ln["x1"], 2), round(ln["bottom"], 2)],
                                    "type": "line", "width": ln.get("linewidth") or 0, "page": i + 1})
            pd.char_count = sum(len(s.text) for s in pd.spans)
            pages.append(pd)
    return pages, {"backend": "pdfplumber", "pages": len(pages), "metadata": {}}


def _read_pdftotext(path: Path) -> tuple[list[PageData], dict]:
    """最后的退路：只有纯文本，没有坐标。结构信息会缺失，必须如实标注。"""
    out = subprocess.run(["pdftotext", "-layout", str(path), "-"],
                         capture_output=True, text=True, timeout=300)
    raw = out.stdout
    chunks = raw.split("\f")
    pages: list[PageData] = []
    for i, ch in enumerate(chunks):
        if not ch.strip() and i == len(chunks) - 1:
            continue
        pd = PageData(number=i + 1, width=595, height=842)
        for ln_no, line in enumerate(ch.splitlines()):
            if not line.strip():
                continue
            pd.spans.append(Span(text=line, bbox=(0, ln_no * 12.0, len(line) * 6.0, ln_no * 12.0 + 12),
                                 size=10, bold=False, italic=False, font="pdftotext",
                                 page=i + 1, line_key=f"{i+1}:{ln_no}"))
        pd.char_count = sum(len(s.text) for s in pd.spans)
        pages.append(pd)
    return pages, {"backend": "pdftotext", "pages": len(pages), "metadata": {},
                   "degraded": "no coordinate layout available"}


def read_pdf(path: str | Path) -> tuple[list[PageData], dict]:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"input pdf not found: {path}")
    errors = []
    for name, fn in (("pymupdf", _read_pymupdf), ("pdfplumber", _read_pdfplumber),
                     ("pdftotext", _read_pdftotext)):
        try:
            pages, meta = fn(path)
            if pages and sum(p.char_count for p in pages) > 0:
                meta["probe"] = probe_backends()
                meta["backend_chain"] = errors + [name]
                return pages, meta
            errors.append(f"{name}: no text")
        except Exception as e:                      # noqa: BLE001 - 后端各异，一律降级
            errors.append(f"{name}: {type(e).__name__}: {e}")
    raise RuntimeError("no usable PDF backend; tried " + "; ".join(errors))


def render_page_png(path: Path, page_no: int, out_png: Path, dpi: int = 150) -> bool:
    """把某页渲染成 PNG（给扫描件 OCR / 图表留证用）。失败返回 False，不抛。"""
    try:
        import pymupdf
        doc = pymupdf.open(path)
        pix = doc[page_no - 1].get_pixmap(dpi=dpi)
        out_png.parent.mkdir(parents=True, exist_ok=True)
        pix.save(out_png)
        return True
    except Exception:
        pass
    if shutil.which("pdftoppm"):
        try:
            subprocess.run(["pdftoppm", "-f", str(page_no), "-l", str(page_no), "-r", str(dpi),
                            "-png", str(path), str(out_png.with_suffix(""))],
                           check=True, timeout=300, capture_output=True)
            return out_png.exists()
        except Exception:
            return False
    return False


if __name__ == "__main__":
    print(json.dumps(probe_backends(), indent=2, ensure_ascii=False))
