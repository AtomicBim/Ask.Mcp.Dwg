"""
ingest — разбор проекта: штамп по графам, текстовый слой, классификация
листов, наследование наименований по шифру, лексический поиск.

Быстрый путь: документ открывается один раз, get_text один раз на страницу,
find_tables НЕ вызывается (ленивые таблицы через sheet_tables()).
Листы с /Rotate 90/270: рамки строк переводятся в повёрнутую систему page.rect.

CLI:
    python ingest.py КЖ2.pdf --q "армирование колонн"
    python ingest.py КЖ2.pdf --titles
    python ingest.py КЖ2.pdf --dump-stamp 45
    python ingest.py КЖ2.pdf --tables 45
    python ingest.py КЖ2.pdf --out ./cards
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import pymupdf as fitz

from dwg_l0 import MM, RenderOpts, fmt_name, plan_sheet, sha16

logging.getLogger("pymupdf").setLevel(logging.ERROR)
try:
    fitz.TOOLS.mupdf_display_errors(False)
except Exception:
    pass

log = logging.getLogger("ingest")

__all__ = ["SourcePdf", "Sheet", "Project", "build_project", "sheet_tables", "Bm25",
           "page_lines", "parse_stamp",
           "KIND_TITLE", "KIND_OD", "KIND_DWG", "KIND_NODE", "KIND_SPEC",
           "KIND_LIST", "KIND_ANNEX", "KIND_SCAN", "KIND_OTHER"]

# --- штамп --------------------------------------------------------------
STAMP_W_MM, STAMP_H_MM = 185.0, 55.0
STAMP_PAD = 1.12
TOL_PT = 5.0                      # допуск сопоставления графы и значения

SECTIONS = ("ПЗУ", "ГП", "АР", "АС", "КЖ", "КМ", "КМД", "КР", "ОВ", "ВК", "ЭОМ",
            "ЭС", "ЭГ", "СС", "АТХ", "ТХ", "ПОС", "ООС", "ПБ", "НВК", "АК", "СД",
            "ТС", "ГСВ", "ПС", "АУПТ", "СОУЭ", "ВН", "ЭН", "ТКР", "ИЛО", "ЭМ")
SEC_RE = re.compile(r"^((?:%s))\d{0,2}$" % "|".join(SECTIONS))
RE_CODE = re.compile(r"[0-9А-ЯA-Z]{2,}(?:[-–—.][0-9А-ЯA-Z]{1,10}){1,6}")
RE_STAGE_VAL = re.compile(r"^[А-Я]{1,3}$")
RE_NUM_VAL = re.compile(r"^\d{1,4}(?:[.,]\d{1,3})?$")
RE_FIO = re.compile(r"^[А-Я][а-я]+(?:\s+[А-Я]\.\s?[А-Я]?\.?)?$")
RE_DATE = re.compile(r"^\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}$")

# строки, которые никогда не являются наименованием
BAD_TITLE = re.compile(
    r"(?i)^(?:формат\b|кол\.?\s*уч|изм\.?$|подп\.?$|дата$|стади|лист(?:ов)?\b|"
    r"№\s*док|взам\.?\s*инв|инв\.?\s*№|подп\.?\s*и\s*дата|разраб|провер|пров\.?$|"
    r"н\.?\s*контр|т\.?\s*контр|нач\.?\s*отдел|гл\.?\s*(?:инж|специал)|"
    r"главный\s+инженер|гип\b|гап\b|утв|согласов|стр\.?\s*\d)")

# --- типы листов --------------------------------------------------------
KIND_TITLE = "титул"
KIND_OD = "ОД"
KIND_DWG = "чертёж"
KIND_NODE = "узел"
KIND_SPEC = "СО"
KIND_LIST = "ведомость"
KIND_ANNEX = "приложение"
KIND_SCAN = "скан"
KIND_OTHER = "прочее"

RE_LIST = re.compile(r"(?i)ведомост\w*\s+(?:рабочих\s+)?чертеж|содержание\s+том|"
                     r"ведомост\w*\s+ссылочн|ведомост\w*\s+основных\s+комплект")
RE_ANNEX = re.compile(r"(?i)паспорт|сертификат\b|техническ\w*\s+услови|руководство\s+по|"
                      r"декларац|опросный\s+лист|протокол\s+испытан|заключение\b")
RE_NODE = re.compile(r"(?i)\bузл(?:ы|а|ов)?\b|\bузел\b|деталь\b|фрагмент\b")
RE_OD = re.compile(r"(?i)общие\s+данные|общие\s+указани|основные\s+показател")

# --- предметные сущности ------------------------------------------------
RE_ELEV = re.compile(r"[+\-]\d{1,2}[.,]\d{3}")
RE_DIA = re.compile(r"(?:[ØøΦ⌀∅]|(?<![A-Za-zА-Яа-я])[dD])\s?\d{1,3}(?:[×x]\d{1,2}(?:[.,]\d)?)?")
RE_REBAR = re.compile(r"(?:[АA]\s?-?\s?(?:240|400|500)|[ВB]\s?-?\s?500)\s?[СCПп]?")
RE_CONCRETE = re.compile(r"\b(?:В\s?\d{1,2}(?:[.,]\d)?|F\s?\d{2,3}|W\s?\d{1,2})\b")
RE_STD = re.compile(r"(?i)\b(?:ГОСТ\s?Р?|СП|СНиП|Серия|ТУ|СТО)\s?\d[\d\-./]{1,20}")
RE_SCALE = re.compile(r"(?i)\b(?:М\s?)?1\s?[:.]\s?(\d{1,4})\b")
RE_SYS = re.compile(r"(?i)\b(?:систем\w*\s+)?(?:П|В|ДВ|ДП|ПД|СИД|К|ОВ)\s?-?\s?\d{1,2}\b")
RE_MARK = re.compile(r"\b(?:КР|КМ|ПР|ПЛ|БР|СТ|ФМ|ФБ|ПС|МЕ|КВ|К|Б|П|Ф|М|Г|Р|Д|Н|С)"
                     r"\s?-?\s?\d{1,3}(?:\.\d{1,2})?\b")

TEXT_CAP = 24000
NOTES_CAP = 2500
TABLES_MAX = 12
TABLE_MD_CAP = 3000
TABLE_MIN_ROWS, TABLE_MIN_COLS, TABLE_MIN_FILL = 3, 2, 0.12

# качество текстового слоя: битые шрифты без ToUnicode дают «("() ## + / (-»
TEXT_MIN_CHARS = 40
TEXT_MIN_ALNUM = 0.55
TEXT_MIN_WORDS = 3

CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\r]")
TEXT_FLAGS = (fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_PRESERVE_LIGATURES
              | fitz.TEXT_MEDIABOX_CLIP)          # без TEXT_PRESERVE_IMAGES


def _s(x: str, cap: int = 200) -> str:
    """Санитайзер: никаких управляющих символов в однострочных полях."""
    return re.sub(r"\s{2,}", " ", CTRL.sub(" ", str(x or ""))).strip()[:cap]


def _numkey(s: str) -> Tuple[int, ...]:
    """'1.10' > '1.2' > '1' — сравнение номеров листов как кортежей."""
    return tuple(int(x) for x in re.findall(r"\d+", s or "")) or (10 ** 6,)


# ======================================================================
@dataclass(frozen=True)
class SourcePdf:
    name: str
    data: bytes
    sha: str = ""

    def with_sha(self) -> "SourcePdf":
        return self if self.sha else SourcePdf(self.name, self.data, sha16(self.data))


@dataclass
class Line:
    text: str
    size: float
    rect: fitz.Rect


@dataclass
class Sheet:
    sid: str
    file: str
    file_sha: str
    page: int
    pages_total: int
    fmt: str
    kind: str = KIND_OTHER
    is_scan: bool = False
    frames: int = 1
    ppm: float = 0.0
    dup_of: Optional[str] = None
    # штамп
    code: str = ""
    section: str = ""
    sheet_no: str = ""
    sheets_total: str = ""
    stage: str = ""
    obj: str = ""
    title: str = ""
    title_src: str = ""            # stamp | headline | inherit | —
    headline: str = ""
    stamp_raw: str = ""
    # содержание
    scales: List[str] = field(default_factory=list)
    systems: List[str] = field(default_factory=list)
    marks: List[str] = field(default_factory=list)
    elevations: List[str] = field(default_factory=list)
    standards: List[str] = field(default_factory=list)
    notes: str = ""
    text: str = ""
    text_len: int = 0
    tables_md: List[str] = field(default_factory=list)

    def name(self) -> str:
        if self.is_scan and not self.title:
            return "без текстового слоя (только изображение)"
        t = self.title or self.headline or "без названия"
        if self.sheet_no and self.title_src == "inherit":
            t = f"{t} (лист {self.sheet_no})"
        return t

    def toc_row(self) -> str:
        return " | ".join([
            self.sid, self.kind, self.section or "—", self.code or "—",
            f"л.{self.sheet_no}" if self.sheet_no else f"стр.{self.page + 1}",
            self.fmt, _s(self.name(), 90),
            f"{self.frames} кадр." + ("" if not self.is_scan else " скан"),
        ])

    def card(self, with_text: bool = True) -> str:
        b = [f"# {self.sid} · {self.code or self.file} · {self.name()}"]
        meta = [f"тип {self.kind}", f"файл {self.file} стр.{self.page + 1}/{self.pages_total}",
                f"формат {self.fmt}"]
        if self.section:
            meta.insert(1, f"раздел {self.section}")
        if self.stage:
            meta.append(f"стадия {self.stage}")
        if self.sheet_no:
            meta.append(f"лист {self.sheet_no}"
                        + (f" из {self.sheets_total}" if self.sheets_total else ""))
        b.append(" · ".join(meta))
        if self.obj:
            b.append(f"объект: {self.obj}")
        if self.headline and self.headline != self.title:
            b.append(f"надпись на листе: {self.headline}")
        for cap, seq in (("масштабы", self.scales), ("системы", self.systems),
                         ("марки", self.marks), ("отметки", self.elevations),
                         ("нормативы", self.standards)):
            if seq:
                b.append(f"{cap}: " + " ".join(seq))
        if self.notes:
            b.append("\n## Примечания (текстовый слой)\n" + self.notes)
        if self.tables_md:
            b.append(f"\n## Таблицы текстового слоя ({len(self.tables_md)})")
            for i, md in enumerate(self.tables_md, 1):
                b.append(f"\n### Таблица {i}\n{md}")
        if self.stamp_raw:
            b.append("\n## Штамп, сырой текст (для сверки)\n```\n"
                     + self.stamp_raw + "\n```")
        if with_text and self.text:
            b.append("\n## Полный текстовый слой листа\n" + self.text)
        return "\n".join(b)

    def fields(self) -> Dict[str, str]:
        """Поля для взвешенного индекса."""
        return {
            "head": " ".join([self.sid, self.code, self.section, self.title,
                              self.headline, self.kind]),
            "meta": " ".join(self.systems + self.marks + self.elevations
                             + self.standards + self.scales + [self.obj]),
            "body": " ".join([self.notes, self.text]),
        }

    def to_dict(self) -> dict:
        return dict(self.__dict__)


# ======================================================================
# BM25 со взвешиванием полей и токенизацией под шифры
# ======================================================================
class Bm25:
    K1, B = 1.5, 0.75
    W = {"head": 4, "meta": 2, "body": 1}
    _SPLIT = re.compile(r"[^0-9a-zа-яё]+")

    @staticmethod
    def tok(s: str) -> List[str]:
        s = (s or "").lower().replace("ё", "е")
        s = s.replace("ø", "d").replace("⌀", "d").replace("∅", "d")
        out: List[str] = []
        for t in Bm25._SPLIT.split(s):
            if not t or (len(t) < 2 and not t.isdigit()):
                continue
            out.append(t)
            if re.fullmatch(r"[a-zа-я]+\d+[a-zа-я]*", t):
                for p in re.findall(r"[a-zа-я]+|\d+", t):
                    if len(p) > 1 or p.isdigit():
                        out.append(p)
        return out

    def __init__(self, docs: Sequence[Dict[str, str]]):
        self.docs: List[Counter] = []
        for d in docs:
            c: Counter = Counter()
            for fname, w in self.W.items():
                for t in self.tok(d.get(fname, "")):
                    c[t] += w
            self.docs.append(c)
        self.len = [sum(c.values()) or 1 for c in self.docs]
        self.avg = sum(self.len) / max(1, len(self.len))
        self.df: Counter = Counter()
        for c in self.docs:
            self.df.update(c.keys())
        self.N = max(1, len(self.docs))

    def top(self, query: str, k: int = 12, boost: Optional[List[float]] = None
            ) -> List[Tuple[int, float]]:
        q = [t for t in self.tok(query) if t in self.df]
        if not q:
            return []
        sc: List[Tuple[int, float]] = []
        for i, c in enumerate(self.docs):
            s = 0.0
            for t in q:
                f = c.get(t, 0)
                if not f:
                    continue
                idf = math.log(1 + (self.N - self.df[t] + 0.5) / (self.df[t] + 0.5))
                s += idf * f * (self.K1 + 1) / (
                    f + self.K1 * (1 - self.B + self.B * self.len[i] / self.avg))
            if s > 0:
                sc.append((i, s * (boost[i] if boost else 1.0)))
        sc.sort(key=lambda x: -x[1])
        return sc[:k]


@dataclass
class Project:
    pid: str
    sheets: List[Sheet]
    files: List[Tuple[str, str, int]]
    warnings: List[str] = field(default_factory=list)
    _bm25: Optional[Bm25] = None

    def sheet(self, sid: str) -> Optional[Sheet]:
        s = str(sid).strip().upper()
        return next((x for x in self.sheets if x.sid == s), None)

    def kinds(self) -> Dict[str, int]:
        return dict(Counter(x.kind for x in self.sheets))

    def sections(self) -> Dict[str, int]:
        return dict(Counter(x.section or "—" for x in self.sheets))

    def toc_md(self) -> str:
        b = [f"ПРОЕКТ {self.pid} · листов {len(self.sheets)} · файлов {len(self.files)}",
             "разделы: " + ", ".join(f"{k} ({v})" for k, v in sorted(self.sections().items())),
             "состав: " + ", ".join(f"{k} ({v})" for k, v in sorted(self.kinds().items())),
             "",
             "Лист | Тип | Разд. | Шифр | № | Формат | Наименование | Нарезка",
             "-" * 110]
        b += [s.toc_row() for s in self.sheets]
        if self.warnings:
            b += [""] + [f"! {w}" for w in self.warnings]
        return "\n".join(b)

    def search(self, query: str, section: Optional[str] = None,
               kind: Optional[str] = None, limit: int = 8
               ) -> List[Tuple[Sheet, float, List[str]]]:
        if self._bm25 is None:
            self._bm25 = Bm25([s.fields() for s in self.sheets])
        # ведомость матчится на всё: она содержит названия всех листов
        boost = [0.35 if s.kind == KIND_LIST else 1.0 for s in self.sheets]
        hits = self._bm25.top(query, k=limit * 4, boost=boost)
        toks = set(Bm25.tok(query))
        out = []
        for i, score in hits:
            sh = self.sheets[i]
            if section and (sh.section or "").upper() != section.strip().upper():
                continue
            if kind and sh.kind != kind:
                continue
            out.append((sh, round(score, 2), _snippets(sh, toks)))
            if len(out) >= limit:
                break
        return out


def _snippets(sh: Sheet, toks: set, limit: int = 3, width: int = 190) -> List[str]:
    src = "\n".join([sh.notes, "\n".join(sh.tables_md), sh.text])
    out: List[str] = []
    for line in (l.strip() for l in src.split("\n")):
        if len(line) < 6:
            continue
        low = line.lower().replace("ё", "е")
        if any(t in low for t in toks):
            out.append(_s(line, width))
            if len(out) >= limit:
                break
    return out


# ======================================================================
# чтение страницы
# ======================================================================
def page_lines(page) -> Tuple[List[Line], str]:
    """Один get_text на страницу: строки с кеглями и плоский текст.
    Для /Rotate 90/270 PyMuPDF отдаёт текст в неповёрнутой системе —
    переводим рамки в систему page.rect, где штамп в правом нижнем углу."""
    lines: List[Line] = []
    parts: List[str] = []
    try:
        d = page.get_text("dict", flags=TEXT_FLAGS)
    except Exception:
        return lines, ""
    rot = page.rotation_matrix if page.rotation else None
    for b in d.get("blocks", []):
        if b.get("type") != 0:
            continue
        for l in b.get("lines", []):
            sp = l.get("spans", [])
            t = re.sub(r"\s{2,}", " ", " ".join(x.get("text", "") for x in sp)).strip()
            if not t:
                continue
            size = max([float(x.get("size") or 0) for x in sp] or [0.0])
            r = fitz.Rect(l["bbox"])
            if rot is not None:
                r = r * rot
            lines.append(Line(text=t, size=size, rect=r))
            parts.append(t)
        parts.append("")
    return lines, CTRL.sub(" ", "\n".join(parts)).strip()


def _text_ok(text: str) -> bool:
    """Текстовый слой пригоден: достаточно букв/цифр и есть настоящие слова."""
    ns = re.sub(r"\s", "", text or "")
    if len(ns) < TEXT_MIN_CHARS:
        return False
    alnum = sum(1 for ch in ns if ch.isalnum())
    if alnum / len(ns) < TEXT_MIN_ALNUM:
        return False
    return len(re.findall(r"[А-Яа-яЁёA-Za-z]{3,}", text)) >= TEXT_MIN_WORDS


def _stamp_rect(page) -> fitz.Rect:
    pr = page.rect
    w = min(pr.width * 0.95, STAMP_W_MM * MM * STAMP_PAD)
    h = min(pr.height * 0.6, STAMP_H_MM * MM * STAMP_PAD)
    return fitz.Rect(pr.x1 - w, pr.y1 - h, pr.x1, pr.y1) & pr


def _header(lines: Sequence[Line], word: str) -> Optional[Line]:
    """Ячейка-заголовок графы: текст равен слову (с точностью до пунктуации)."""
    w = word.lower()
    best = None
    for l in lines:
        t = l.text.strip(" .:;№").lower()
        if t == w and (best is None or l.rect.y0 < best.rect.y0):
            best = l
    return best


def _value_for(hdr: Line, lines: Sequence[Line], pat: re.Pattern) -> Optional[Line]:
    """Значение графы: под заголовком (приоритет) или справа от него."""
    hcx = (hdr.rect.x0 + hdr.rect.x1) / 2.0
    hcy = (hdr.rect.y0 + hdr.rect.y1) / 2.0
    cands: List[Tuple[int, float, Line]] = []
    for l in lines:
        if l is hdr:
            continue
        t = l.text.strip(" .:;")
        if not pat.fullmatch(t):
            continue
        cx = (l.rect.x0 + l.rect.x1) / 2.0
        if l.rect.y0 >= hdr.rect.y1 - 2.0 and abs(cx - hcx) <= max(TOL_PT, hdr.rect.width):
            cands.append((0, abs(cx - hcx) + (l.rect.y0 - hdr.rect.y1) * 0.1, l))
        elif (l.rect.x0 >= hdr.rect.x1 - 2.0
              and abs((l.rect.y0 + l.rect.y1) / 2.0 - hcy) <= hdr.rect.height):
            cands.append((1, l.rect.x0 - hdr.rect.x1, l))
    if not cands:
        return None
    cands.sort(key=lambda x: (x[0], x[1]))
    return cands[0][2]


def _pick_code(lines: Sequence[Line], right_x: float) -> Tuple[str, str]:
    best, bscore = "", -1.0
    for l in lines:
        if l.rect.x1 < right_x:
            continue
        for m in RE_CODE.finditer(l.text.upper()):
            c = m.group(0)
            parts = re.split(r"[-–—.]", c)
            sec = next((SEC_RE.match(p).group(1) for p in parts if SEC_RE.match(p)), "")
            score = len(parts) + (6.0 if sec else 0.0) + l.size / 10.0
            if score > bscore:
                best, bscore = c, score
    sec = ""
    if best:
        for p in re.split(r"[-–—.]", best):
            m = SEC_RE.match(p)
            if m:
                sec = m.group(1)
                break
    return best, sec


def _ok_name(l: Line, code: str) -> bool:
    t = l.text.strip(" .:;")
    if len(t) < 3 or BAD_TITLE.match(t) or RE_FIO.match(t) or RE_DATE.match(t):
        return False
    if code and code.upper() in t.upper():
        return False
    return not re.fullmatch(r"[\d\s.,№/\-]+", t)


def _join(ls: Sequence[Line]) -> str:
    return _s(" ".join(l.text for l in sorted(ls, key=lambda l: (l.rect.y0, l.rect.x0))), 200)


def _stamp_names(ins: Sequence[Line], sr: fitz.Rect, h_st: Line,
                 vals: Sequence[Optional[Line]], code: str) -> Tuple[str, str]:
    """Форма 3: средняя колонка между «Дата» и «Стадия».
    Выше строки «Стадия» — объект (гр.2), на её уровне — здание (гр.3),
    ниже строки значений — наименование изображения (гр.4)."""
    h_dt = _header(ins, "дата")
    x_l = (h_dt.rect.x1 + 2.0) if h_dt else sr.x0 + 0.40 * sr.width
    x_r = h_st.rect.x0 + 2.0
    y_row = h_st.rect.y0 - 2.0
    y_val = max([v.rect.y1 for v in vals if v] or [h_st.rect.y1 + 14.0]) + 1.0
    c = [l for l in ins if _ok_name(l, code) and l.rect.x0 >= x_l - 4.0]
    top = [l for l in c if l.rect.y1 <= y_row + 4.0]
    mid = [l for l in c if l.rect.x1 <= x_r and y_row <= l.rect.y0 < y_val]
    low = [l for l in c if l.rect.x1 <= x_r and l.rect.y0 >= y_val]
    obj = " · ".join(x for x in (_join(top), _join(mid)) if x)
    return _join(low), obj


def parse_stamp(page, lines: Sequence[Line]) -> dict:
    sr = _stamp_rect(page)
    ins = [l for l in lines if fitz.Rect(l.rect).intersects(sr)]
    raw = "\n".join(l.text for l in ins)[:1600]

    code, section = _pick_code(ins, sr.x0 + 0.30 * sr.width)
    if not code:
        code, section = _pick_code(ins, sr.x0)

    h_st, h_l, h_ls = (_header(ins, w) for w in ("стадия", "лист", "листов"))
    v_st = _value_for(h_st, ins, RE_STAGE_VAL) if h_st else None
    v_l = _value_for(h_l, ins, RE_NUM_VAL) if h_l else None
    v_ls = _value_for(h_ls, ins, RE_NUM_VAL) if h_ls else None
    stage = _s(v_st.text.strip(" .:;"), 4) if v_st else ""
    sheet_no = _s(v_l.text.strip(" .:;"), 8) if v_l else ""
    sheets_total = _s(v_ls.text.strip(" .:;"), 8) if v_ls else ""
    if sheet_no and sheets_total and _numkey(sheet_no) > _numkey(sheets_total):
        sheet_no, sheets_total = sheets_total, sheet_no

    # наименование только из формы 3/5 (есть «Стадия»); форма 6 -> наследование
    title, obj = (_stamp_names(ins, sr, h_st, (v_st, v_l, v_ls), code)
                  if h_st else ("", ""))
    return {"code": _s(code, 60), "section": section, "stage": stage,
            "sheet_no": sheet_no, "sheets_total": sheets_total,
            "title": title, "obj": obj, "stamp_raw": raw}


def _headline(page, lines: Sequence[Line]) -> str:
    sr = _stamp_rect(page)
    best, bs = "", 0.0
    for l in lines:
        if fitz.Rect(l.rect).intersects(sr):
            continue
        t = l.text.strip(" .:;")
        if len(t) < 8 or len(t) > 160 or RE_FIO.match(t) or BAD_TITLE.match(t):
            continue
        if l.size > bs:
            best, bs = t, l.size
    return _s(best, 160)


def _notes(text: str) -> str:
    m = re.search(r"(?im)^\s*(примечани\w*|общие\s+указани\w*|указани\w*)\b", text)
    if not m:
        return ""
    return CTRL.sub(" ", text[m.start(): m.start() + NOTES_CAP]).strip()


def _entities(text: str) -> dict:
    def uniq(seq, cap):
        seen, out = set(), []
        for x in seq:
            v = re.sub(r"\s+", "", str(x))
            k = v.upper()
            if k and k not in seen:
                seen.add(k)
                out.append(v)
            if len(out) >= cap:
                break
        return out

    return {
        "scales": uniq([f"1:{m.group(1)}" for m in RE_SCALE.finditer(text)], 12),
        "systems": uniq([m.group(0) for m in RE_SYS.finditer(text)], 30),
        "marks": uniq(list(RE_DIA.findall(text))
                      + [m.group(0) for m in RE_REBAR.finditer(text)]
                      + [m.group(0) for m in RE_CONCRETE.finditer(text)]
                      + [m.group(0) for m in RE_MARK.finditer(text)], 60),
        "elevations": uniq(RE_ELEV.findall(text), 24),
        "standards": uniq([m.group(0) for m in RE_STD.finditer(text)], 24),
    }


def _classify(sh: Sheet, headline: str) -> str:
    code = (sh.code or "").upper()
    probe = " ".join([headline, sh.title, sh.text[:2500]]).lower()
    short = max(sh.fmt.find("A4"), sh.fmt.find("A3")) >= 0
    if sh.is_scan:
        return KIND_SCAN
    if sh.page <= 2 and not sh.code:
        return KIND_TITLE
    if code.endswith(".СО") or "спецификация оборудован" in (
            sh.title + " " + headline).lower():
        return KIND_SPEC
    if RE_LIST.search(probe):
        return KIND_LIST
    if not sh.code and RE_ANNEX.search(probe):
        return KIND_ANNEX
    if RE_OD.search(probe) or (short and sh.text_len > 1500):
        return KIND_OD
    if RE_NODE.search(headline.lower() or "") or RE_NODE.search(sh.title.lower() or ""):
        return KIND_NODE
    if any(k in sh.fmt for k in ("A0", "A1", "A2")) or (sh.text_len and sh.text_len < 1500):
        return KIND_DWG
    return KIND_OTHER


# ======================================================================
# ленивые таблицы
# ======================================================================
def _stamp_top(page, lines: Sequence[Line], code: str) -> Optional[float]:
    """Верх штампа (в системе page.rect): строка с шифром минус ~4 мм."""
    if not code:
        return None
    sr = _stamp_rect(page)
    ys = [l.rect.y0 for l in lines
          if fitz.Rect(l.rect).intersects(sr) and code.upper() in l.text.upper()]
    return (min(ys) - 4.0 * MM) if ys else None


def _find_tables(page, clip: Optional[fitz.Rect]):
    kw = {"clip": clip} if clip is not None else {}
    try:
        return page.find_tables(strategy="lines", **kw)
    except TypeError:
        return page.find_tables(**kw)


def sheet_tables(pdf: bytes, page_index: int) -> List[str]:
    """find_tables только по требованию и только выше штампа: иначе рамка
    и штамп склеиваются со спецификацией в одну таблицу на весь лист."""
    doc = fitz.open(stream=pdf, filetype="pdf")
    out: List[str] = []
    try:
        page = doc[max(0, min(page_index, len(doc) - 1))]
        lines, _ = page_lines(page)
        top = _stamp_top(page, lines, parse_stamp(page, lines)["code"])
        clip = None
        if top is not None and top > page.rect.y0 + 20:
            pr = page.rect
            clip = fitz.Rect(pr.x0, pr.y0, pr.x1, top) * page.derotation_matrix
        tabs = list(getattr(_find_tables(page, clip), "tables", []) or [])
        if not tabs and clip is not None:
            tabs = list(getattr(_find_tables(page, None), "tables", []) or [])
        for t in tabs:
            if int(getattr(t, "row_count", 0) or 0) < TABLE_MIN_ROWS:
                continue
            if int(getattr(t, "col_count", 0) or 0) < TABLE_MIN_COLS:
                continue
            try:
                cells = t.extract() or []
            except Exception:
                cells = []
            total = sum(len(r) for r in cells) or 1
            filled = sum(1 for r in cells for c in r if c and str(c).strip())
            if filled / total < TABLE_MIN_FILL:
                log.info("стр.%s: таблица %sx%s отброшена, заполнено %.2f",
                         page_index + 1, t.row_count, t.col_count, filled / total)
                continue
            try:
                md = (t.to_markdown() or "").strip()
            except Exception:
                md = ""
            if len(md) > 40:
                out.append(CTRL.sub(" ", md)[:TABLE_MD_CAP])
            if len(out) >= TABLES_MAX:
                break
    except Exception as e:
        log.warning("find_tables стр.%s: %r", page_index + 1, e)
    finally:
        doc.close()
    return out


# ======================================================================
# сборка проекта
# ======================================================================
def build_project(sources: Sequence[SourcePdf], opts: RenderOpts = RenderOpts()
                  ) -> Project:
    srcs = [s.with_sha() for s in sources]
    pid = sha16("|".join(sorted(s.sha for s in srcs)).encode())
    sheets: List[Sheet] = []
    files: List[Tuple[str, str, int]] = []
    n = 0
    for s in srcs:
        doc = fitz.open(stream=s.data, filetype="pdf")           # один раз на файл
        try:
            total = len(doc)
            files.append((s.name, s.sha, total))
            for pi in range(total):
                page = doc[pi]
                pr = page.rect
                lines, text = page_lines(page)
                good = _text_ok(text)
                if not good:
                    lines, text = [], ""
                st = parse_stamp(page, lines)
                hl = _headline(page, lines)
                cols, rows, ppm, _ = plan_sheet(pr.width / MM, pr.height / MM, opts)
                n += 1
                sh = Sheet(
                    sid=f"L{n}", file=s.name, file_sha=s.sha, page=pi,
                    pages_total=total, fmt=fmt_name(pr.width, pr.height),
                    is_scan=not good, frames=cols * rows, ppm=ppm,
                    code=st["code"], section=st["section"], sheet_no=st["sheet_no"],
                    sheets_total=st["sheets_total"], stage=st["stage"],
                    obj=st["obj"], title=st["title"],
                    title_src="stamp" if st["title"] else "",
                    headline=hl, stamp_raw=st["stamp_raw"],
                    notes=_notes(text), text=text[:TEXT_CAP], text_len=len(text),
                    **_entities(text))
                sh.kind = _classify(sh, hl)
                if not sh.title and hl:
                    sh.title, sh.title_src = hl, "headline"
                sheets.append(sh)
        finally:
            doc.close()
    _inherit_titles(sheets)
    _mark_dups(sheets)
    return Project(pid=pid, sheets=sheets, files=files,
                   warnings=_warnings(sheets))


def _inherit_titles(sheets: List[Sheet]) -> None:
    """Форма 6 не содержит наименования: наследуем от первого листа документа."""
    groups: Dict[str, List[Sheet]] = {}
    for sh in sheets:
        if sh.code:
            groups.setdefault(sh.code.upper(), []).append(sh)
    for _code, g in groups.items():
        g.sort(key=lambda x: (_numkey(x.sheet_no), x.page))
        donor = next((x for x in g if x.title_src == "stamp"), None)
        if donor is None:
            continue
        for sh in g:
            if sh is donor:
                continue
            if sh.title_src in ("", "headline") and (
                    not sh.title or sh.title == sh.headline):
                sh.title, sh.title_src = donor.title, "inherit"
            if not sh.obj:
                sh.obj = donor.obj
            if not sh.stage:
                sh.stage = donor.stage
            if not sh.sheets_total:
                sh.sheets_total = donor.sheets_total
            if sh.kind in (KIND_OTHER, KIND_DWG) and donor.kind == KIND_SPEC:
                sh.kind = KIND_SPEC


def _mark_dups(sheets: List[Sheet]) -> None:
    first: Dict[str, str] = {}
    for sh in sheets:
        if sh.text_len < 40:
            continue
        h = sha16((re.sub(r"\s+", " ", sh.text[:4000]) + "|" + sh.fmt)
                  .encode("utf-8", "ignore"))
        if h in first:
            sh.dup_of = first[h]
        else:
            first[h] = sh.sid


def _warnings(sheets: List[Sheet]) -> List[str]:
    w: List[str] = []
    no_stamp = (KIND_SCAN, KIND_TITLE, KIND_ANNEX)
    secs = {k: v for k, v in Counter(x.section for x in sheets).items() if k}
    if len(secs) > 1:
        w.append("смешаны разделы: " + ", ".join(f"{k} ({v} л.)"
                                                 for k, v in sorted(secs.items()))
                 + " — отвечай только по загруженным")
    nsc = sum(1 for x in sheets if x.is_scan)
    if nsc:
        w.append(f"{nsc} лист(ов) без пригодного текстового слоя — по ним только изображение")
    nt = [x.sid for x in sheets if not x.title and x.kind not in no_stamp]
    if nt:
        w.append(f"у {len(nt)} лист(ов) не определено наименование: "
                 + ", ".join(nt[:12]) + (" …" if len(nt) > 12 else ""))
    nn = [x.sid for x in sheets if not x.sheet_no and x.kind not in no_stamp]
    if nn:
        w.append(f"у {len(nn)} лист(ов) не прочитан номер листа по штампу: "
                 + ", ".join(nn[:12]) + (" …" if len(nn) > 12 else ""))
    d = [x.sid for x in sheets if x.dup_of]
    if d:
        w.append(f"дубли по содержимому: {', '.join(d[:12])}"
                 + (" …" if len(d) > 12 else ""))
    return w


# ======================================================================
def _main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="ingest: разбор проекта")
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--out", default="")
    ap.add_argument("--q", default="")
    ap.add_argument("--section", default="")
    ap.add_argument("--kind", default="")
    ap.add_argument("--titles", action="store_true", help="сводка по наименованиям")
    ap.add_argument("--dump-stamp", type=int, default=0)
    ap.add_argument("--tables", type=int, default=0)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    if a.verbose:
        log.setLevel(logging.INFO)

    srcs = []
    for p in a.paths:
        with open(p, "rb") as fh:
            srcs.append(SourcePdf(name=os.path.basename(p), data=fh.read()))

    if a.dump_stamp:
        doc = fitz.open(stream=srcs[0].data, filetype="pdf")
        page = doc[a.dump_stamp - 1]
        lines, text = page_lines(page)
        sr = _stamp_rect(page)
        print(f"rot {page.rotation} · rect {page.rect} · текст пригоден: {_text_ok(text)}")
        print("--- строки в зоне штампа: кегль | x0,y0 | текст ---")
        for l in sorted((x for x in lines if fitz.Rect(x.rect).intersects(sr)),
                        key=lambda x: (round(x.rect.y0), x.rect.x0)):
            print(f"{l.size:5.1f} | {l.rect.x0 - sr.x0:6.1f},{l.rect.y0 - sr.y0:6.1f} | {l.text}")
        print("--- разбор граф ---")
        print(json.dumps(parse_stamp(page, lines), ensure_ascii=False, indent=2))
        print("--- надпись на листе ---")
        print(_headline(page, lines))
        doc.close()
        return 0

    if a.tables:
        tabs = sheet_tables(srcs[0].data, a.tables - 1)
        if not tabs:
            print("таблиц не найдено (подробности: -v)")
        for i, md in enumerate(tabs, 1):
            print(f"=== таблица {i} ===\n{md}\n")
        return 0

    t0 = time.monotonic()
    pr = build_project(srcs)
    dt = time.monotonic() - t0

    if a.titles:
        print(f"{'лист':<6}{'тип':<11}{'№':<7}{'ист.':<9}наименование")
        for s in pr.sheets:
            print(f"{s.sid:<6}{s.kind:<11}{s.sheet_no or '—':<7}"
                  f"{s.title_src or '—':<9}{_s(s.name(), 70)}")
        if pr.warnings:
            print("\n" + "\n".join(f"! {w}" for w in pr.warnings))
    else:
        print(pr.toc_md())
    print(f"\nразобрано за {dt:.1f} с "
          f"({dt / max(1, len(pr.sheets)) * 1000:.0f} мс/лист)")

    if a.out:
        os.makedirs(a.out, exist_ok=True)
        data_by_sha = {x.with_sha().sha: x.data for x in srcs}
        for sh in pr.sheets:
            if sh.kind in (KIND_SPEC, KIND_LIST):
                sh.tables_md = sheet_tables(data_by_sha[sh.file_sha], sh.page)
            with open(os.path.join(a.out, f"{sh.sid}.md"), "w", encoding="utf-8") as fh:
                fh.write(sh.card())
        with open(os.path.join(a.out, "project.json"), "w", encoding="utf-8") as fh:
            json.dump({"pid": pr.pid,
                       "files": [{"name": n, "sha": s, "pages": p} for n, s, p in pr.files],
                       "warnings": pr.warnings,
                       "sheets": [s.to_dict() for s in pr.sheets]},
                      fh, ensure_ascii=False, indent=2)
        print(f"-> {a.out}")

    if a.q:
        print(f"\n--- поиск: «{a.q}» ---")
        for sh, score, sn in pr.search(a.q, a.section or None, a.kind or None):
            print(f"{sh.sid} [{score}] {sh.kind} · {sh.code} · {_s(sh.name(), 70)}")
            for x in sn:
                print(f"    … {x}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
