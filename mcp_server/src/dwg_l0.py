"""
dwg_l0 — геометрия и растеризация чертежа.

Принцип: фиксированная плотность px/мм. Число кадров определяется тем,
сколько нужно, чтобы лист при этой плотности влез в предел размера
изображения модели (MAX_IMG_PX). Лист делится РАВНОМЕРНО на это число,
кадры расширяются нахлёстом на внутренние стороны.

    A4 -> 1   A3 -> 1   A2 -> 4   A1 -> 6   A0 -> 12   (при 8 px/мм, 3000 px)

Контракт: наружу — только frozen dataclass / tuple / bytes. Ни один
pymupdf-объект модуль не покидает. Координаты — доли габарита листа
(x0, y0, x1, y1) в повёрнутой системе page.rect, x слева->вправо, y сверху->вниз.
Растр (get_pixmap) работает в повёрнутой системе, текст (get_text) — в
неповёрнутой, поэтому clip для текста переводится через derotation_matrix.

CLI:
    python dwg_l0.py drawing.pdf --page 43 --out ./t --map
    python dwg_l0.py drawing.pdf --stats
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import logging
import math
import os
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

import pymupdf as fitz
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

logging.getLogger("pymupdf").setLevel(logging.ERROR)
try:
    fitz.TOOLS.mupdf_display_errors(False)
except Exception:
    pass

__all__ = [
    "MM", "PX_PER_MM", "MAX_IMG_PX", "RenderOpts", "Tile", "SheetRender",
    "fmt_name", "plan_sheet", "render_sheet", "render_region", "render_overview",
    "render_sheet_doc", "render_region_doc",
    "render_map", "sha16", "page_geometry",
]

MM = 72.0 / 25.4

# --- две настройки качества, больше ничего ------------------------------
PX_PER_MM = 8.0          # 8 px/мм ~= 203 dpi: текст 1.8 мм даёт ~14 px
MAX_IMG_PX = 3000        # предел стороны одного изображения для модели

OVERLAP_FRAC = 0.14      # нахлёст между соседями, доля ядра кадра
SINGLE_TOLERANCE = 1.18  # до этого превышения отдаём один кадр, чуть сжав
MAX_AXIS = 8
TILE_Q = 88
OVERVIEW_PX, OVERVIEW_Q = 1600, 92
MAP_PX, MAP_Q = 1100, 62
DPI_HARD_CAP = 600
INK_DPI = 110
INK_MAX_PX = 2400
INK_THR = 245
TILE_MIN_INK = 0.0006
FRAME_ONLY_INK = 0.003
TILE_TEXT_CAP = 1400

GOST_FMT = (
    ("A0", 1189, 841), ("A1", 841, 594), ("A2", 594, 420),
    ("A3", 420, 297), ("A4", 297, 210),
)

Box = Tuple[float, float, float, float]


# ======================================================================
@dataclass
class RenderOpts:
    px_per_mm: float = PX_PER_MM
    max_img_px: int = MAX_IMG_PX
    tile_q: int = TILE_Q
    overview_px: int = OVERVIEW_PX
    tile_text: bool = True
    native_cap: bool = True


@dataclass(frozen=True)
class Tile:
    label: str
    jpeg: bytes
    px: Tuple[int, int]
    dpi: int
    row: int
    col: int
    rows: int
    cols: int
    nbox: Box
    core: Box
    ink: float
    text: str = ""

    @property
    def kb(self) -> int:
        return len(self.jpeg) // 1024

    def b64(self) -> str:
        return base64.b64encode(self.jpeg).decode()

    def manifest(self) -> dict:
        return {"label": self.label, "px": list(self.px), "dpi": self.dpi,
                "row": self.row, "col": self.col, "rows": self.rows,
                "cols": self.cols, "nbox": [round(v, 4) for v in self.nbox],
                "core": [round(v, 4) for v in self.core], "kb": self.kb,
                "text_len": len(self.text)}


@dataclass(frozen=True)
class SheetRender:
    sid: str
    page: int
    fmt: str
    ppm: float
    single: bool
    overview: bytes
    overview_px: Tuple[int, int]
    tiles: Tuple[Tile, ...]
    skipped: Tuple[str, ...] = ()
    notes: Tuple[str, ...] = ()

    @property
    def nbytes(self) -> int:
        return len(self.overview) + sum(len(t.jpeg) for t in self.tiles)

    @property
    def kb(self) -> int:
        return self.nbytes // 1024


# ======================================================================
def sha16(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16]


def fmt_name(w_pt: float, h_pt: float) -> str:
    w_mm, h_mm = w_pt / MM, h_pt / MM
    lo, hi = min(w_mm, h_mm), max(w_mm, h_mm)
    best = ""
    for nm, L, S in GOST_FMT:
        if abs(hi - L) <= 0.06 * L and abs(lo - S) <= 0.06 * S:
            best = nm
            break
    if not best:
        for nm, L, S in GOST_FMT:
            for k in (2, 3, 4, 5):
                if abs(hi - L * k) <= 0.06 * L * k and abs(lo - S) <= 0.06 * S:
                    best = f"{nm}x{k}"
                    break
            if best:
                break
    ori = "гориз." if w_mm >= h_mm else "верт."
    return f"{best + ' ' if best else ''}{ori} {int(round(hi))}×{int(round(lo))}"


def page_geometry(pdf: bytes, page_index: int) -> Tuple[float, float, str]:
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        pr = doc[max(0, min(page_index, len(doc) - 1))].rect
        return round(pr.width / MM, 1), round(pr.height / MM, 1), fmt_name(pr.width, pr.height)
    finally:
        doc.close()


def _nb(r: fitz.Rect, pr: fitz.Rect) -> Box:
    return (round(max(0.0, (r.x0 - pr.x0) / pr.width), 4),
            round(max(0.0, (r.y0 - pr.y0) / pr.height), 4),
            round(min(1.0, (r.x1 - pr.x0) / pr.width), 4),
            round(min(1.0, (r.y1 - pr.y0) / pr.height), 4))


def _rect(b: Box, pr: fitz.Rect) -> fitz.Rect:
    x0, x1 = sorted((float(b[0]), float(b[2])))
    y0, y1 = sorted((float(b[1]), float(b[3])))
    return fitz.Rect(pr.x0 + max(0.0, x0) * pr.width, pr.y0 + max(0.0, y0) * pr.height,
                     pr.x0 + min(1.0, x1) * pr.width,
                     pr.y0 + min(1.0, y1) * pr.height) & pr


def _clean(s: str, cap: int) -> str:
    s = (s or "").replace("\r", "\n")
    return re.sub(r"\n{3,}", "\n\n", s.strip())[:cap]


def _text_in(page, rect: fitz.Rect) -> str:
    """Текст в прямоугольнике повёрнутой системы: для get_text clip
    переводится в неповёрнутую (для rotation=0 матрица единичная)."""
    try:
        return page.get_text("text", clip=rect * page.derotation_matrix) or ""
    except Exception:
        return ""


# ======================================================================
# план нарезки — вся арифметика
# ======================================================================
def plan_sheet(w_mm: float, h_mm: float, opts: RenderOpts = RenderOpts()
               ) -> Tuple[int, int, float, bool]:
    """(cols, rows, фактическая плотность px/мм, один_кадр)."""
    mx = float(opts.max_img_px)
    w_px, h_px = w_mm * opts.px_per_mm, h_mm * opts.px_per_mm
    # один кадр, если лист почти влезает: небольшое сжатие дешевле деления
    if max(w_px, h_px) <= mx * SINGLE_TOLERANCE:
        ppm = min(opts.px_per_mm, mx / max(1e-6, max(w_mm, h_mm)))
        return 1, 1, round(ppm, 3), True
    eff = mx / (1.0 + OVERLAP_FRAC)
    cols = max(1, min(MAX_AXIS, math.ceil(w_px / eff)))
    rows = max(1, min(MAX_AXIS, math.ceil(h_px / eff)))
    return cols, rows, round(opts.px_per_mm, 3), False


def _dpi(ppm: float) -> int:
    return int(max(36, min(DPI_HARD_CAP, round(ppm * 25.4))))


def _native_dpi(page) -> int:
    best = 0.0
    try:
        for im in page.get_images(full=True):
            w = float(im[2] or 0)
            if w > 0:
                best = max(best, w * 72.0 / max(1.0, page.rect.width))
    except Exception:
        pass
    return int(best)


def _is_scan(page) -> bool:
    try:
        t = page.get_text("text") or ""
    except Exception:
        t = ""
    if len(t.strip()) >= 60:
        return False
    try:
        return bool(page.get_images())
    except Exception:
        return True


class _Ink:
    def __init__(self, page):
        pr = page.rect
        side = max(pr.width, pr.height) or 1.0
        dpi = int(max(24.0, min(float(INK_DPI), INK_MAX_PX * 72.0 / side)))
        try:
            pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
            img = Image.frombytes("L", [pix.width, pix.height], pix.samples)
            self.m = np.asarray(img.filter(ImageFilter.MinFilter(3))) < INK_THR
        except Exception:
            self.m = np.ones((8, 8), dtype=bool)
        self.pr = pr
        self.kx = self.m.shape[1] / pr.width
        self.ky = self.m.shape[0] / pr.height

    def frac(self, r: fitz.Rect) -> float:
        h, w = self.m.shape
        x0 = max(0, int((r.x0 - self.pr.x0) * self.kx))
        y0 = max(0, int((r.y0 - self.pr.y0) * self.ky))
        x1 = min(w, int(math.ceil((r.x1 - self.pr.x0) * self.kx)))
        y1 = min(h, int(math.ceil((r.y1 - self.pr.y0) * self.ky)))
        if x1 - x0 < 2 or y1 - y0 < 2:
            return 0.0
        s = self.m[y0:y1, x0:x1]
        return float(s.sum()) / float(max(1, s.size))


# ======================================================================
def _jpeg(img: Image.Image, q: int) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=q, optimize=True)
    return buf.getvalue()


def _raster(page, rect: fitz.Rect, dpi: int, q: int, max_side: Optional[int] = None,
            sharpen: bool = False) -> Tuple[bytes, int, int, int]:
    pix = page.get_pixmap(dpi=int(dpi), clip=rect, colorspace=fitz.csRGB)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    if sharpen:
        try:
            img = img.filter(ImageFilter.MinFilter(3))
        except Exception:
            pass
    if max_side and max(img.size) > max_side:
        k = max_side / max(img.size)
        img = img.resize((max(1, int(img.width * k)), max(1, int(img.height * k))),
                         Image.LANCZOS)
    eff = int(round(int(dpi) * img.width / float(max(1, pix.width))))
    return _jpeg(img, q), img.width, img.height, eff


def _frames(area: fitz.Rect, cols: int, rows: int
            ) -> List[Tuple[int, int, fitz.Rect, fitz.Rect]]:
    """Равномерное деление + нахлёст только на внутренние стороны."""
    out = []
    cw, ch = area.width / cols, area.height / rows
    ox, oy = cw * OVERLAP_FRAC / 2.0, ch * OVERLAP_FRAC / 2.0
    for r in range(rows):
        for c in range(cols):
            core = fitz.Rect(area.x0 + c * cw, area.y0 + r * ch,
                             area.x0 + (c + 1) * cw, area.y0 + (r + 1) * ch)
            frame = fitz.Rect(core.x0 - (ox if c else 0.0),
                              core.y0 - (oy if r else 0.0),
                              core.x1 + (ox if c < cols - 1 else 0.0),
                              core.y1 + (oy if r < rows - 1 else 0.0)) & area
            out.append((r + 1, c + 1, core, frame))
    return out


def _tiles(page, area: fitz.Rect, ink: _Ink, ppm: float, cols: int, rows: int,
           opts: RenderOpts, prefix: str, scan: bool
           ) -> Tuple[Tuple[Tile, ...], Tuple[str, ...]]:
    pr = page.rect
    dpi = _dpi(ppm)
    out: List[Tile] = []
    skipped: List[str] = []
    single = (cols == 1 and rows == 1)
    for r, c, core, frame in _frames(area, cols, rows):
        label = prefix if single else f"{prefix}R{r}C{c}"
        f = ink.frac(core)
        if f < TILE_MIN_INK and not single:
            skipped.append(label)
            continue
        txt = _clean(_text_in(page, core), TILE_TEXT_CAP)
        if not single and not scan and not txt and f < FRAME_ONLY_INK:
            skipped.append(label)
            continue
        jpg, w, h, eff = _raster(page, frame, dpi, opts.tile_q,
                                 max_side=opts.max_img_px)
        out.append(Tile(label=label, jpeg=jpg, px=(w, h), dpi=eff, row=r, col=c,
                        rows=rows, cols=cols, nbox=_nb(frame, pr),
                        core=_nb(core, pr), ink=round(f, 4),
                        text=txt if opts.tile_text else ""))
    return tuple(out), tuple(skipped)


# ======================================================================
def render_overview(pdf: bytes, page_index: int, opts: RenderOpts = RenderOpts()
                    ) -> Tuple[bytes, int, int]:
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        page = doc[max(0, min(page_index, len(doc) - 1))]
        side = max(page.rect.width, page.rect.height) or 1.0
        dpi = int(max(72, min(DPI_HARD_CAP, 2 * opts.overview_px * 72.0 / side)))
        return _raster(page, page.rect, dpi, OVERVIEW_Q,
                       max_side=opts.overview_px, sharpen=True)[:3]
    finally:
        doc.close()


def render_sheet_doc(doc, page_index: int, sid: str = "L1",
                     opts: RenderOpts = RenderOpts()) -> SheetRender:
    """render_sheet на уже открытом документе (воркеры держат его открытым)."""
    page = doc[max(0, min(page_index, len(doc) - 1))]
    pr = page.rect
    notes: List[str] = []
    scan = _is_scan(page)
    cols, rows, ppm, single = plan_sheet(pr.width / MM, pr.height / MM, opts)
    if scan:
        nd = _native_dpi(page)
        if opts.native_cap and nd >= 72 and _dpi(ppm) > nd * 1.15:
            ppm = max(2.0, nd * 1.15 / 25.4)
            notes.append(f"скан: плотность ограничена нативной ~{nd} dpi")
        else:
            notes.append("скан без текстового слоя")
    if single and ppm < opts.px_per_mm - 0.05:
        notes.append(f"один кадр при {ppm} px/мм вместо {opts.px_per_mm}")
    if page.rotation:
        notes.append(f"лист повёрнут на {page.rotation}°")
    ink = _Ink(page)
    tiles, skipped = _tiles(page, pr, ink, ppm, cols, rows, opts, sid, scan)
    side = max(pr.width, pr.height) or 1.0
    ov_dpi = int(max(72, min(DPI_HARD_CAP, 2 * opts.overview_px * 72.0 / side)))
    ov, ow, oh, _ = _raster(page, pr, ov_dpi, OVERVIEW_Q,
                            max_side=opts.overview_px, sharpen=True)
    return SheetRender(sid=sid, page=page.number,
                       fmt=fmt_name(pr.width, pr.height), ppm=ppm, single=single,
                       overview=ov, overview_px=(ow, oh), tiles=tiles,
                       skipped=skipped, notes=tuple(notes))


def render_sheet(pdf: bytes, page_index: int, sid: str = "L1",
                 opts: RenderOpts = RenderOpts()) -> SheetRender:
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        return render_sheet_doc(doc, page_index, sid, opts)
    finally:
        doc.close()


def render_region_doc(doc, page_index: int, box: Box, sid: str = "L1",
                      opts: RenderOpts = RenderOpts(), zoom: float = 2.0
                      ) -> Tuple[Tile, ...]:
    """Доразглядывание на открытом документе: плотность в zoom раз выше."""
    page = doc[max(0, min(page_index, len(doc) - 1))]
    area = _rect(box, page.rect)
    if not area.is_valid or area.width < 8 or area.height < 8:
        return ()
    o = RenderOpts(px_per_mm=opts.px_per_mm * max(1.0, zoom),
                   max_img_px=opts.max_img_px, tile_q=opts.tile_q,
                   overview_px=opts.overview_px, tile_text=opts.tile_text,
                   native_cap=opts.native_cap)
    cols, rows, ppm, _ = plan_sheet(area.width / MM, area.height / MM, o)
    ink = _Ink(page)
    return _tiles(page, area, ink, ppm, cols, rows, o, f"{sid}Z",
                  _is_scan(page))[0]


def render_region(pdf: bytes, page_index: int, box: Box, sid: str = "L1",
                  opts: RenderOpts = RenderOpts(), zoom: float = 2.0
                  ) -> Tuple[Tile, ...]:
    """Доразглядывание: плотность в zoom раз выше, тот же принцип деления."""
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        return render_region_doc(doc, page_index, box, sid, opts, zoom)
    finally:
        doc.close()


def render_map(sr: SheetRender, max_px: int = MAP_PX, q: int = MAP_Q) -> bytes:
    try:
        img = Image.open(io.BytesIO(sr.overview)).convert("RGB")
        if img.width > max_px:
            k = max_px / img.width
            img = img.resize((max_px, max(1, int(img.height * k))), Image.LANCZOS)
        W, H = img.size
        d = ImageDraw.Draw(img)
        for t in sr.tiles:
            d.rectangle([t.nbox[0] * W, t.nbox[1] * H, t.nbox[2] * W, t.nbox[3] * H],
                        outline=(0, 120, 255), width=1)
            d.rectangle([t.core[0] * W, t.core[1] * H, t.core[2] * W, t.core[3] * H],
                        outline=(150, 205, 255), width=1)
            d.text((t.core[0] * W + 3, t.core[1] * H + 2), t.label, fill=(0, 90, 200))
        return _jpeg(img, q)
    except Exception:
        return sr.overview


# ======================================================================
def _main() -> int:
    ap = argparse.ArgumentParser(description="dwg_l0: нарезка и план")
    ap.add_argument("path")
    ap.add_argument("--page", type=int, default=1)
    ap.add_argument("--out", default="")
    ap.add_argument("--px-per-mm", type=float, default=PX_PER_MM)
    ap.add_argument("--max-img-px", type=int, default=MAX_IMG_PX)
    ap.add_argument("--map", action="store_true")
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()

    with open(a.path, "rb") as fh:
        pdf = fh.read()
    opts = RenderOpts(px_per_mm=a.px_per_mm, max_img_px=a.max_img_px)

    if a.stats:
        doc = fitz.open(stream=pdf, filetype="pdf")
        print(f"страниц: {len(doc)} · {a.px_per_mm} px/мм · предел {a.max_img_px} px")
        tot = 0
        for i, page in enumerate(doc):
            pr = page.rect
            c, r, ppm, single = plan_sheet(pr.width / MM, pr.height / MM, opts)
            tot += c * r
            print(f"  стр.{i + 1:>4} {fmt_name(pr.width, pr.height):<22} "
                  f"{c}×{r}={c * r:<3} кадр(ов) @{ppm} px/мм"
                  + ("  [один кадр]" if single else "")
                  + (f"  rot {page.rotation}" if page.rotation else ""))
        doc.close()
        print(f"итого кадров на весь документ: {tot}")
        return 0

    sr = render_sheet(pdf, a.page - 1, f"L{a.page}", opts)
    print(f"{sr.sid}: {sr.fmt} @{sr.ppm} px/мм · кадров {len(sr.tiles)} "
          f"(пусто {len(sr.skipped)}) · {sr.kb} КБ")
    for n in sr.notes:
        print(f"  ! {n}")
    for t in sr.tiles:
        print(f"  {t.label} {t.px[0]}×{t.px[1]} @{t.dpi}dpi ink={t.ink} "
              f"текст={len(t.text)} симв · {t.kb} КБ")

    if a.out:
        os.makedirs(a.out, exist_ok=True)
        with open(os.path.join(a.out, f"{sr.sid}_overview.jpg"), "wb") as fh:
            fh.write(sr.overview)
        for t in sr.tiles:
            with open(os.path.join(a.out, f"{t.label}.jpg"), "wb") as fh:
                fh.write(t.jpeg)
        if a.map:
            with open(os.path.join(a.out, f"{sr.sid}_map.jpg"), "wb") as fh:
                fh.write(render_map(sr))
        with open(os.path.join(a.out, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump({"sid": sr.sid, "fmt": sr.fmt, "ppm": sr.ppm,
                       "tiles": [t.manifest() for t in sr.tiles],
                       "skipped": list(sr.skipped), "notes": list(sr.notes)},
                      fh, ensure_ascii=False, indent=2)
        print(f"-> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
