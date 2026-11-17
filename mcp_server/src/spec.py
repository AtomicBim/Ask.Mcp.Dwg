"""
spec — строки спецификации (СО) по графам ГОСТ 21.110.

Что делает детерминированно (чтобы не делала модель):
  * находит шапку таблицы и сопоставляет графы по тексту заголовков;
  * заголовок системы («Система П1», «ДВ1», «Отопление») действует на все
    строки ниже до следующего заголовка — в том числе через границу листа;
  * склеивает перенесённые строки (нет поз./ед./кол. -> продолжение предыдущей);
  * каждой строке проставляет лист СО (штамп), страницу PDF и sid;
  * считает суммы по системам и изделиям.

CLI:
    python spec.py АТОМ-055-1.1-ОВ.PDF                  # сводка разбора
    python spec.py АТОМ-055-1.1-ОВ.PDF --sheet 45       # строки страницы PDF 45
    python spec.py АТОМ-055-1.1-ОВ.PDF --totals system
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import pymupdf as fitz

from ingest import (KIND_SPEC, Project, Sheet, SourcePdf, build_project,
                    find_page_tables, _numkey)

log = logging.getLogger("spec")

COLS = ("pos", "name", "mark", "code", "maker", "unit", "qty", "mass", "note")
# порядок важен: «Масса единицы» раньше «Единица», «Код» раньше «Тип/марка»
HDR = (("pos", r"^поз"), ("name", r"наимен"), ("code", r"^код"), ("mass", r"масс"),
       ("mark", r"тип|марк|обознач"), ("maker", r"завод|изготов|поставщ"),
       ("qty", r"^кол|количеств"), ("unit", r"^ед|единиц"), ("note", r"примеч"))
DEFAULT_MAP = {k: i for i, k in enumerate(COLS)}

_SC = r"(?:ДВ|ДП|ПД|СИД|ВЕ|ПЕ|ПВ|ВД|КВ|П|В|К|Т|О)"
SYS_HDR = re.compile(
    r"(?i)^\s*(?:систем\w*|отоплени\w*|вентиляци\w*|противодымн\w*|теплоснабжени\w*|"
    r"индивидуальн\w+\s+тепл\w*|итп\b|тепловой\s+пункт|узел\s+учет\w*|общеобменн\w*|"
    r"дымоудалени\w*|подпор\w*|кондиционир\w*|холодоснабжени\w*)"
    rf"|^\s*{_SC}\s?-?\d{{1,3}}[а-яa-z]?(?:\s*[,;и\-–]\s*{_SC}?\s?-?\d{{1,3}}[а-яa-z]?)*\s*[.:]?\s*$")
GRP_HDR = re.compile(
    r"(?i)^(?:оборудование|изделия|материалы|изделия\s+и\s+материалы|арматура|"
    r"трубопровод\w*|трубы|воздуховод\w*|фасонн\w*(?:\s+\w+)?|изоляци\w*|теплоизоляци\w*|"
    r"креплени\w*|огнезащит\w*|клапан\w*|решетк\w*|решётк\w*|воздухораспределител\w*|"
    r"прочее|монтажные\s+издели\w*|приборы\s+отоплени\w*|отопительные\s+приборы)\s*[:.]?$")


@dataclass
class SpecRow:
    rid: str
    code: str            # шифр документа СО
    so_sheet: str        # лист СО по штампу
    pdf_page: int        # страница PDF (1-based)
    sid: str
    system: str = ""
    group: str = ""
    pos: str = ""
    name: str = ""
    mark: str = ""
    art: str = ""        # графа «Код оборудования, изделия, материала»
    maker: str = ""
    unit: str = ""
    qty: str = ""
    mass: str = ""
    note: str = ""
    wrapped: int = 0
    flags: List[str] = field(default_factory=list)

    @property
    def qty_num(self) -> Optional[float]:
        return _num(self.qty)


@dataclass
class SpecResult:
    rows: List[SpecRow]
    docs: Dict[str, Tuple[str, str, str, str]]   # шифр -> (лист от, лист до, sid от, sid до)
    systems: List[str]
    report: Dict[str, List[str]]
    seconds: float


# ----------------------------------------------------------------------
def _cell(x) -> str:
    s = "" if x is None else str(x)
    s = re.sub(r"-\s*\n\s*(?=[а-яё])", "", s)          # «воздухо-\nвод» -> «воздуховод»
    return re.sub(r"\s+", " ", s).strip()


def _num(s: str) -> Optional[float]:
    s = (s or "").strip().replace("\u00a0", " ")
    if not re.fullmatch(r"\d[\d ]*(?:[.,]\d+)?", s):
        return None
    try:
        return float(s.replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def _is_hdr_line(vals: Sequence[str]) -> bool:
    """Шапка или строка нумерации граф «1 2 3 … 9» (повтор на листе)."""
    ne = [v for v in vals if v]
    if not ne:
        return True
    if len(ne) >= 5 and all(re.fullmatch(r"\d{1,2}", v) for v in ne):
        ints = [int(v) for v in ne]
        if ints == list(range(ints[0], ints[0] + len(ints))):
            return True
    if len(ne) >= 3 and all(any(re.search(p, v.lower()) for _, p in HDR) for v in ne):
        return True
    return False


def _skip_hdr(cells: List[list], i: int) -> int:
    while i < len(cells) and _is_hdr_line([_cell(x) for x in cells[i]]):
        i += 1
    return i


def _map_header(cells: List[list]) -> Tuple[Optional[Dict[str, int]], int]:
    for h in range(min(6, len(cells))):
        low = [_cell(x).lower() for x in cells[h]]
        if not any("наимен" in c for c in low):
            continue
        m: Dict[str, int] = {}
        for rr in (h, h + 1):                   # шапка бывает в две строки
            if rr >= len(cells):
                break
            for j, c in enumerate(_cell(x).lower() for x in cells[rr]):
                if not c or j in m.values():
                    continue
                for key, pat in HDR:
                    if key not in m and re.search(pat, c):
                        m[key] = j
                        break
        if "name" in m and len(m) >= 3:
            return m, _skip_hdr(cells, h + 1)
    return None, 0


def _page_tables(page) -> Tuple[List[List[list]], bool]:
    """Таблицы только выше штампа (иначе find_tables склеивает рамку и штамп).
    -> (таблицы, empty_grid_skipped): пустой бланк отсекается префильтром."""
    tabs, skipped = find_page_tables(page)
    out: List[List[list]] = []
    for t in sorted(tabs, key=lambda t: (round(t.bbox[1]), t.bbox[0])):
        if (t.col_count or 0) < 4 or (t.row_count or 0) < 2:
            continue
        try:
            out.append(t.extract() or [])
        except Exception as e:
            log.warning("стр.%s: extract: %r", page.number + 1, e)
    return out, skipped


def page_tables_cells(doc, page_index: int) -> Tuple[List[List[list]], bool]:
    """Для воркера: ячейки таблиц страницы (picklable) + флаг пропуска пустого бланка."""
    try:
        return _page_tables(doc[page_index])
    except Exception as e:
        log.warning("стр.%s: %r", page_index + 1, e)
        return [], False


# ----------------------------------------------------------------------
TablesBySid = Dict[str, Tuple[List[List[list]], bool]]


def spec_sheets(pr: Project) -> List[Sheet]:
    """Листы СО, не сканы: только их таблицы нужно извлекать."""
    return [sh for sh in pr.sheets if sh.kind == KIND_SPEC and not sh.is_scan]


def build_spec(pr: Project, pdf: bytes) -> SpecResult:
    """Последовательная сборка (CLI/эталон). Параллельная — workers.build_spec_parallel."""
    t0 = time.monotonic()
    tabs: TablesBySid = {}
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        for sh in spec_sheets(pr):
            tabs[sh.sid] = page_tables_cells(doc, sh.page)
    finally:
        doc.close()
    return assemble_spec(pr, tabs, t0)


def assemble_spec(pr: Project, tables_by_sid: TablesBySid,
                  t0: Optional[float] = None) -> SpecResult:
    """Свёртка (последовательная): шапки, система/группа, перенос через лист."""
    if t0 is None:
        t0 = time.monotonic()
    rep: Dict[str, List[str]] = {"сканы": [], "без таблицы": [], "графы не распознаны": [],
                                 "шапка взята с предыдущего листа": [], "пустые бланки": []}
    by_code: "OrderedDict[str, List[Sheet]]" = OrderedDict()
    for sh in pr.sheets:
        if sh.kind == KIND_SPEC:
            by_code.setdefault((sh.code or "без шифра").upper(), []).append(sh)

    rows: List[SpecRow] = []
    docs: Dict[str, Tuple[str, str, str, str]] = {}
    systems: List[str] = []
    if by_code:
        for code, g in by_code.items():
            g.sort(key=lambda s: (_numkey(s.sheet_no), s.page))
            docs[code] = (g[0].sheet_no, g[-1].sheet_no, g[0].sid, g[-1].sid)
            system = group = ""
            prev: Optional[SpecRow] = None
            last_map: Optional[Dict[str, int]] = None
            last_n = 0
            for sh in g:
                if sh.is_scan:
                    rep["сканы"].append(sh.sid)
                    continue
                tables, skipped = tables_by_sid.get(sh.sid, ([], False))
                if not tables:
                    rep["пустые бланки" if skipped else "без таблицы"].append(sh.sid)
                    continue
                k = 0
                for cells in tables:
                    ncol = max((len(r) for r in cells), default=0)
                    m, start = _map_header(cells)
                    if m is None:
                        if last_map is not None and last_n == ncol:
                            m, start = last_map, _skip_hdr(cells, 0)
                            rep["шапка взята с предыдущего листа"].append(sh.sid)
                        elif ncol == len(COLS):
                            m, start = DEFAULT_MAP, _skip_hdr(cells, 0)
                        else:
                            rep["графы не распознаны"].append(f"{sh.sid} ({ncol} гр.)")
                    else:
                        last_map, last_n = m, ncol
                    for r in cells[start:]:
                        vals = [_cell(x) for x in r]
                        if not any(vals) or _is_hdr_line(vals):
                            continue
                        if m is None:
                            k += 1
                            prev = SpecRow(rid=f"{sh.sid}-{k}", code=code, so_sheet=sh.sheet_no,
                                           pdf_page=sh.page + 1, sid=sh.sid, system=system,
                                           group=group, name=" | ".join(v for v in vals if v),
                                           flags=["графы не распознаны"])
                            rows.append(prev)
                            continue
                        f = {key: (vals[j] if j < len(vals) else "") for key, j in m.items()}
                        name, pos = f.get("name", ""), f.get("pos", "")
                        # объединённая ячейка заголовка системы начинается с графы «Поз.»
                        if pos and not name and not re.fullmatch(r"[\w.\-/]{1,8}", pos):
                            name, pos = pos, ""
                        qty, unit, mark = f.get("qty", ""), f.get("unit", ""), f.get("mark", "")
                        no_data = not qty and not unit and not pos
                        if no_data and name and not mark:
                            if SYS_HDR.search(name):
                                system, group = name, ""
                                if name not in systems:
                                    systems.append(name)
                                continue
                            if len(name) <= 45 and GRP_HDR.match(name):
                                group = name
                                continue
                        if no_data and prev is not None:          # перенос строки
                            for key, attr in (("name", "name"), ("mark", "mark"), ("code", "art"),
                                              ("maker", "maker"), ("mass", "mass"), ("note", "note")):
                                v = name if key == "name" else f.get(key, "")
                                if v:
                                    setattr(prev, attr, (getattr(prev, attr) + " " + v).strip())
                            prev.wrapped += 1
                            fl = f"продолжение на {sh.sid}"
                            if prev.sid != sh.sid and fl not in prev.flags:
                                prev.flags.append(fl)
                            continue
                        k += 1
                        row = SpecRow(rid=f"{sh.sid}-{k}", code=code, so_sheet=sh.sheet_no,
                                      pdf_page=sh.page + 1, sid=sh.sid, system=system, group=group,
                                      pos=pos, name=name, mark=mark, art=f.get("code", ""),
                                      maker=f.get("maker", ""), unit=unit, qty=qty,
                                      mass=f.get("mass", ""), note=f.get("note", ""))
                        if qty and _num(qty) is None:
                            row.flags.append("кол-во не число")
                        if not name:
                            row.flags.append("нет наименования")
                        rows.append(row)
                        prev = row
    rep = {k: v for k, v in rep.items() if v}
    return SpecResult(rows=rows, docs=docs, systems=systems, report=rep,
                      seconds=round(time.monotonic() - t0, 1))


# ----------------------------------------------------------------------
def filter_rows(res: SpecResult, code: str = "", sheet_from: str = "", sheet_to: str = "",
                system: str = "", query: str = "") -> List[SpecRow]:
    lo = _numkey(sheet_from) if sheet_from else None
    hi = _numkey(sheet_to) if sheet_to else None
    toks = [t for t in re.split(r"\s+", query.lower().replace("ё", "е")) if t]
    out = []
    for r in res.rows:
        if code and code.upper() not in r.code:
            continue
        if lo is not None and _numkey(r.so_sheet) < lo:
            continue
        if hi is not None and _numkey(r.so_sheet) > hi:
            continue
        if system and system.lower() not in r.system.lower():
            continue
        if toks:
            hay = " ".join([r.name, r.mark, r.art, r.maker, r.note, r.system]).lower().replace("ё", "е")
            if not all(t in hay for t in toks):
                continue
        out.append(r)
    return out


def _e(v) -> str:
    s = ", ".join(v) if isinstance(v, list) else str(v if v is not None else "")
    return s.replace("|", "\\|").replace("\n", " ")


_MD = (("rid", "rid"), ("pos", "Поз."), ("name", "Наименование и техн. характеристика"),
       ("mark", "Тип, марка, обозначение"), ("art", "Код"), ("maker", "Завод"),
       ("unit", "Ед."), ("qty", "Кол."), ("mass", "Масса"), ("note", "Прим."),
       ("so_sheet", "Лист СО"), ("pdf_page", "PDF"), ("flags", "Флаги"))


def rows_md(rows: Sequence[SpecRow]) -> str:
    head = "| " + " | ".join(h for _, h in _MD) + " |\n|" + "---|" * len(_MD)
    out: List[str] = []
    cur = None
    for r in rows:
        key = (r.code, r.system, r.group)
        if key != cur:
            cur = key
            out.append(f"\n### [{r.code}] Система: {r.system or '— (до первого заголовка)'}"
                       + (f" · группа: {r.group}" if r.group else ""))
            out.append(head)
        out.append("| " + " | ".join(_e(getattr(r, a)) for a, _ in _MD) + " |")
    return "\n".join(out).strip()


def totals_md(rows: Sequence[SpecRow], by: str = "system") -> str:
    agg: "OrderedDict[tuple, dict]" = OrderedDict()
    for r in rows:
        if by == "item":
            key = (r.name[:140], r.mark[:80])
        elif by == "system_item":
            key = (r.system, r.name[:140], r.mark[:80])
        else:
            key = (r.system or "—",)
        a = agg.setdefault(key, {"sum": OrderedDict(), "n": 0, "bad": []})
        a["n"] += 1
        q = r.qty_num
        if q is None:
            a["bad"].append(r.rid)
        else:
            u = r.unit or "—"
            a["sum"][u] = a["sum"].get(u, 0.0) + q
    cols = {"item": "Наименование | Марка", "system_item": "Система | Наименование | Марка"}.get(by, "Система")
    b = [f"| {cols} | Сумма по ед. изм. | Строк | Кол-во не число (rid) |",
         "|" + "---|" * (cols.count("|") + 4)]
    for key, a in agg.items():
        s = "; ".join(f"{v:g} {u}" for u, v in a["sum"].items()) or "—"
        bad = ", ".join(a["bad"][:10]) + (" …" if len(a["bad"]) > 10 else "")
        b.append("| " + " | ".join(_e(x) for x in key) + f" | {s} | {a['n']} | {bad or '—'} |")
    return "\n".join(b)


# ----------------------------------------------------------------------
def _main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="spec: строки СО")
    ap.add_argument("path")
    ap.add_argument("--sheet", type=int, default=0, help="страница PDF")
    ap.add_argument("--totals", default="", help="system | item | system_item")
    a = ap.parse_args()
    with open(a.path, "rb") as fh:
        data = fh.read()
    pr = build_project([SourcePdf(os.path.basename(a.path), data)])
    res = build_spec(pr, data)
    print(f"строк {len(res.rows)} · систем {len(res.systems)} · {res.seconds} с")
    for c, d in res.docs.items():
        print(f"  {c}: листы {d[0]}–{d[1]} ({d[2]}–{d[3]})")
    for k, v in res.report.items():
        print(f"  ! {k}: {', '.join(v[:20])}")
    print("системы:", " / ".join(res.systems[:40]))
    wr = [r for r in res.rows if r.wrapped]
    print(f"строк со склейкой переносов: {len(wr)}")
    if a.sheet:
        print(rows_md([r for r in res.rows if r.pdf_page == a.sheet]))
    if a.totals:
        print(totals_md(res.rows, a.totals))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
