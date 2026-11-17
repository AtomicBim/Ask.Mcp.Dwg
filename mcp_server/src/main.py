"""
Ask.Mcp.Dwg — MCP-сервер проверки проектной документации по PDF из чата OpenWebUI.

Инструменты:
  list_uploads · open_project · project_map · sheet_text · find ·
  spec_rows · spec_totals · look

Картинки модели НЕ отдаются (OWUI передаёт в контекст только текст):
look режет лист, сам спрашивает Gemini через OpenRouter и возвращает текст.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from typing import Dict, List, Optional, Tuple

import uvicorn
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse
from starlette.routing import Route

import owui
import store
import vision
from dwg_l0 import SheetRender, render_region, render_sheet, sha16
from ingest import (KIND_LIST, KIND_OD, KIND_SPEC, Project, Sheet, SourcePdf,
                    build_project, sheet_tables, _s)
from spec import SpecResult, build_spec, filter_rows, rows_md, totals_md

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("mcp_dwg")

SCHEMA = "v1"                      # поменять, если изменились классы Sheet/SpecRow
SPEC_WAIT_S = float(os.getenv("SPEC_WAIT_S", "100"))
LOOK_MAX_TILES = int(os.getenv("LOOK_MAX_TILES", "12"))
TEXT_MAX = int(os.getenv("SHEET_TEXT_MAX", "40000"))
ROWS_LIMIT = int(os.getenv("SPEC_ROWS_LIMIT", "250"))
REQUIRE_USER = os.getenv("REQUIRE_USER_HEADER", "false").lower() == "true"

mcp = FastMCP("DwgReviewServer", stateless_http=True, json_response=True,
              transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

FITZ = threading.Lock()            # PyMuPDF не потокобезопасен — тяжёлые операции по очереди
_projects = store.Lru(6)
_specs = store.Lru(6)
_spec_tasks: Dict[str, asyncio.Task] = {}
_open_locks: Dict[str, asyncio.Lock] = {}


# ----------------------------------------------------------------------
def _k(*parts: str) -> str:
    return "/".join((SCHEMA,) + parts)


def _user_id(ctx: Context) -> str:
    try:
        req = ctx.request_context.request
        return (req.headers.get("x-openwebui-user-id") or "").strip() if req is not None else ""
    except Exception:
        return ""


def _project(pid: str) -> Project:
    pid = (pid or "").strip()
    pr = _projects.get(pid)
    if pr is None:
        pr = store.load_obj(_k("projects", f"{pid}.pkl"))
        if pr is None:
            raise ValueError(f"проект {pid!r} не открыт: сначала вызови open_project")
        _projects.put(pid, pr)
    return pr


def _pdf(pid: str) -> bytes:
    b = store.load_bytes(_k("pdf", f"{pid}.pdf"))
    if b is None:
        raise ValueError(f"PDF проекта {pid!r} не найден в кэше: вызови open_project повторно")
    return b


def _norm_sid(s: str) -> str:
    s = (s or "").strip().upper().replace("Л", "L")
    s = re.sub(r"^(?:СТР\.?|PAGE|P)\s*", "", s)
    return "L" + s if re.fullmatch(r"\d+", s) else s


def _sheet(pr: Project, sid: str) -> Sheet:
    sh = pr.sheet(_norm_sid(sid))
    if sh is None:
        raise ValueError(f"лист {sid!r} не найден (допустимо L1…L{len(pr.sheets)})")
    return sh


def _sids(pr: Project, spec: str) -> List[Sheet]:
    out: List[Sheet] = []
    for tok in re.split(r"[,\s;]+", spec or ""):
        if not tok:
            continue
        m = re.fullmatch(r"[LlЛл]?(\d+)\s*[-–]\s*[LlЛл]?(\d+)", tok)
        if m:
            a, b = sorted((int(m.group(1)), int(m.group(2))))
            out += [x for x in (pr.sheet(f"L{i}") for i in range(a, b + 1)) if x]
        else:
            out.append(_sheet(pr, tok))
    return out


def _parse_box(box: str) -> Tuple[float, float, float, float]:
    v = [float(x) for x in re.findall(r"-?\d+(?:[.,]\d+)?", box.replace(",", " ").replace(";", " "))
         ] if box else []
    v = [float(str(x).replace(",", ".")) for x in v]
    if len(v) != 4:
        raise ValueError("box: нужно 4 числа x0,y0,x1,y1 в долях листа (0…1)")
    if max(v) > 1.0:
        v = [x / 100.0 for x in v]
    x0, x1 = sorted((v[0], v[2]))
    y0, y1 = sorted((v[1], v[3]))
    return (max(0.0, x0), max(0.0, y0), min(1.0, x1), min(1.0, y1))


# ----------------------------------------------------------------------
# синхронные тяжёлые операции (в потоке, под FITZ)
# ----------------------------------------------------------------------
def _ingest_sync(name: str, data: bytes, sha: str) -> Project:
    with FITZ:
        return build_project([SourcePdf(name=name, data=data, sha=sha)])


def _spec_sync(pid: str) -> SpecResult:
    pr, pdf = _project(pid), _pdf(pid)
    with FITZ:
        res = build_spec(pr, pdf)
    store.save_obj(_k("spec", f"{pid}.pkl"), res)
    log.info("spec %s: %d строк за %s с", pid, len(res.rows), res.seconds)
    return res


def _render_sync(pid: str, sh: Sheet) -> SheetRender:
    key = _k("render", pid, f"{sh.sid}.pkl")
    sr = store.load_obj(key)
    if sr is None:
        pdf = _pdf(pid)
        with FITZ:
            sr = render_sheet(pdf, sh.page, sh.sid)
        store.save_obj(key, sr)
    return sr


def _region_sync(pid: str, sh: Sheet, box, zoom: float):
    pdf = _pdf(pid)
    with FITZ:
        return render_region(pdf, sh.page, box, sh.sid, zoom=zoom)


def _tables_sync(pid: str, sh: Sheet) -> List[str]:
    key = _k("tables", pid, f"{sh.sid}.pkl")
    t = store.load_obj(key)
    if t is None:
        pdf = _pdf(pid)
        with FITZ:
            t = sheet_tables(pdf, sh.page)
        store.save_obj(key, t)
    return t


def _spec_start(pid: str) -> None:
    if _specs.get(pid) is not None or store.exists(_k("spec", f"{pid}.pkl")):
        return
    t = _spec_tasks.get(pid)
    if t is None or (t.done() and (t.cancelled() or t.exception() is not None)):
        _spec_tasks[pid] = asyncio.create_task(asyncio.to_thread(_spec_sync, pid))


async def _spec(pid: str) -> SpecResult:
    res = _specs.get(pid) or store.load_obj(_k("spec", f"{pid}.pkl"))
    if res is not None:
        _specs.put(pid, res)
        return res
    _project(pid)
    _spec_start(pid)
    t = _spec_tasks[pid]
    try:
        res = await asyncio.wait_for(asyncio.shield(t), SPEC_WAIT_S)
    except asyncio.TimeoutError:
        raise RuntimeError("спецификация ещё собирается (первый разбор 1–3 мин): "
                           "повтори этот же вызов через 30–60 с")
    except Exception as e:
        _spec_tasks.pop(pid, None)
        raise RuntimeError(f"ошибка сборки СО: {e!r}")
    _specs.put(pid, res)
    _spec_tasks.pop(pid, None)
    return res


# ======================================================================
# инструменты
# ======================================================================
@mcp.tool()
async def list_uploads(ctx: Context, query: str = "") -> str:
    """
    Список PDF, загруженных в чаты OpenWebUI (новые сверху).
    Вызывай, только если open_project не нашёл файл по имени.
    Args:
        query: часть имени файла, например "АТОМ-055" (необязательно).
    """
    uid = _user_id(ctx)
    if REQUIRE_USER and not uid:
        raise ValueError("не передан пользователь OWUI — список файлов недоступен")
    files = await owui.list_pdfs(uid)
    if query:
        files = [f for f in files if owui._norm(query) in owui._norm(f["name"])]
    if not files:
        return "PDF не найдено. Попроси пользователя загрузить PDF в чат."
    b = ["| file_id | Имя | МБ | Загружен |", "|---|---|---|---|"]
    for f in files[:15]:
        b.append(f"| {f['id']} | {f['name']} | {f['size'] / 1e6:.1f} | "
                 f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(f['created_at']))} |")
    return "\n".join(b)


@mcp.tool()
async def open_project(ctx: Context, file: str = "") -> str:
    """
    Открывает PDF из чата: разбор всех страниц (штамп, текстовый слой, тип листа),
    кэш по хэшу файла. Первый раз ~40 с, повторно мгновенно. В фоне запускает
    сборку строк спецификации для spec_rows.
    Возвращает project (pid) — передавай его во все остальные инструменты.
    Args:
        file: имя PDF (как в чате) или file_id. Пусто — последний загруженный PDF.
    """
    uid = _user_id(ctx)
    if REQUIRE_USER and not uid:
        raise ValueError("не передан пользователь OWUI")
    files = await owui.list_pdfs(uid)
    f, alts = owui.pick(files, file)
    if f is None:
        raise ValueError(f"PDF {file!r} не найден. Вызови list_uploads и уточни имя.")
    t0 = time.monotonic()
    pid = store.load_text(_k("fileid", f"{f['id']}.txt"))
    if not pid or not store.exists(_k("projects", f"{pid}.pkl")):
        data = await owui.download(f["id"])
        sha = sha16(data)
        pid = sha16(sha.encode())                     # как Project.pid в ingest для 1 файла
        lock = _open_locks.setdefault(pid, asyncio.Lock())
        async with lock:
            if not store.exists(_k("projects", f"{pid}.pkl")):
                store.save_bytes(_k("pdf", f"{pid}.pdf"), data)
                pr = await asyncio.to_thread(_ingest_sync, f["name"], data, sha)
                store.save_obj(_k("projects", f"{pid}.pkl"), pr)
                _projects.put(pid, pr)
        store.save_text(_k("fileid", f"{f['id']}.txt"), pid)
    pr = _project(pid)
    _spec_start(pid)

    spec = [s for s in pr.sheets if s.kind == KIND_SPEC]
    codes: Dict[str, List[Sheet]] = {}
    for s in spec:
        codes.setdefault(s.code or "без шифра", []).append(s)
    b = [f"project = {pid}",
         f"файл: {f['name']} · страниц {len(pr.sheets)} · разбор {time.monotonic() - t0:.1f} с",
         "sid Ln = страница PDF n (нумерация PDF, считается с обложки).",
         "разделы: " + ", ".join(f"{k} ({v})" for k, v in sorted(pr.sections().items())),
         "состав: " + ", ".join(f"{k} ({v})" for k, v in sorted(pr.kinds().items()))]
    for c, g in codes.items():
        b.append(f"СО {c}: листов {len(g)} · {g[0].sid}–{g[-1].sid} · "
                 f"лист СО {g[0].sheet_no or '?'}–{g[-1].sheet_no or '?'}")
    if pr.warnings:
        b += [f"! {w}" for w in pr.warnings]
    if alts:
        b.append("другие подходящие файлы: " + "; ".join(a["name"] for a in alts))
    b.append("Строки СО собираются в фоне. Дальше: project_map → sheet_text(ОД) → "
             "spec_rows (порциями до конца) → find/look по схемам.")
    return "\n".join(b)


@mcp.tool()
async def project_map(project: str) -> str:
    """
    Карта документа: sid (=страница PDF) | тип листа | раздел | шифр | лист по штампу |
    формат | наименование | число кадров. Основа для п.2 проверки и таблицы охвата.
    """
    pr = _project(project)
    return "sid Ln = страница PDF n\n" + pr.toc_md()


@mcp.tool()
async def sheet_text(project: str, sids: str, tables: str = "auto",
                     max_chars: int = TEXT_MAX) -> str:
    """
    Текстовый слой листов: штамп, примечания, полный текст, таблицы в Markdown.
    Картинок нет. Для общих данных, ведомостей, примечаний на схемах.
    Args:
        sids: "L6" | "L6,L9" | "L3-L15".
        tables: "auto" (таблицы для ОД/ведомостей/СО) | "yes" | "no".
        max_chars: ограничение объёма ответа.
    """
    pr = _project(project)
    out: List[str] = []
    used = 0
    for sh in _sids(pr, sids):
        if sh.is_scan:
            part = (f"# {sh.sid} · стр. PDF {sh.page + 1} · {sh.name()}\n"
                    "текстового слоя нет — читать только через look")
        else:
            want = tables == "yes" or (tables == "auto" and sh.kind in (KIND_OD, KIND_LIST, KIND_SPEC))
            if want and not sh.tables_md:
                sh.tables_md = await asyncio.to_thread(_tables_sync, project, sh)
            part = f"(стр. PDF {sh.page + 1})\n" + sh.card(with_text=True)
        if used + len(part) > max_chars:
            out.append(f"\n… обрезано на {sh.sid}: запроси остальные листы отдельно")
            break
        out.append(part)
        used += len(part)
    return "\n\n---\n\n".join(out)


@mcp.tool()
async def find(project: str, query: str, kind: str = "", section: str = "",
               limit: int = 8) -> str:
    """
    Лексический поиск (BM25) по листам проекта: марки, системы, шифры, слова.
    Примеры: "ДВ1", "КПД-4 EI 150", "гильза", "ПНОК".
    Args:
        kind: фильтр типа листа: "чертёж" | "ОД" | "СО" | "узел" | "ведомость" | "скан".
        section: фильтр раздела, например "ОВ".
    """
    pr = _project(project)
    hits = pr.search(query, section or None, kind or None, max(1, min(limit, 20)))
    if not hits:
        return (f"по «{query}» совпадений в текстовом слое нет. "
                "Сканы не индексируются — проверь project_map и look.")
    b = []
    for sh, sc, sn in hits:
        b.append(f"{sh.sid} (PDF {sh.page + 1}) [{sc}] {sh.kind} · {sh.code or '—'} · "
                 f"л.{sh.sheet_no or '—'} · {_s(sh.name(), 90)}")
        b += [f"    … {x}" for x in sn]
    return "\n".join(b)


@mcp.tool()
async def spec_rows(project: str, sheet_from: str = "", sheet_to: str = "",
                    system: str = "", query: str = "", code: str = "",
                    offset: int = 0, limit: int = ROWS_LIMIT) -> str:
    """
    Строки спецификации по графам ГОСТ 21.110 с системой (заголовок действует до
    следующего, в т.ч. через границу листа), склеенными переносами, листом СО и
    страницей PDF. Читай ВСЁ порциями: пока в шапке есть next_offset — вызывай снова.
    Args:
        sheet_from / sheet_to: листы СО по штампу, например "1" и "10".
        system: фильтр по подстроке названия системы, например "ДВ1".
        query: слова через пробел, все должны встретиться (наименование/марка/код/завод/прим.).
        code: шифр документа СО, если их несколько.
    Флаги строк: «продолжение на Lx» (перенос через лист), «кол-во не число»,
    «графы не распознаны» — такие строки сверяй через sheet_text или look.
    """
    res = await _spec(project)
    rows = filter_rows(res, code, sheet_from, sheet_to, system, query)
    limit = max(20, min(limit, 600))
    part = rows[offset: offset + limit]
    nxt = offset + len(part)
    head = [f"строк по фильтру {len(rows)} · показаны {offset + 1 if part else 0}–{nxt}"
            + (f" · next_offset={nxt}" if nxt < len(rows) else " · это конец выборки")]
    if offset == 0:
        head.append("документы СО: " + "; ".join(
            f"{c}: листы {d[0]}–{d[1]} ({d[2]}–{d[3]})" for c, d in res.docs.items()))
        head.append("системы: " + " / ".join(res.systems) if res.systems else
                    "заголовки систем не распознаны — система определяется по схемам")
        for k, v in res.report.items():
            head.append(f"! {k}: {', '.join(v[:30])}")
    return "\n".join(head) + "\n\n" + (rows_md(part) if part else "(пусто)")


@mcp.tool()
async def spec_totals(project: str, by: str = "system", system: str = "",
                      query: str = "", code: str = "") -> str:
    """
    Суммы количеств из СО по единицам измерения (без смешения единиц).
    Args:
        by: "system" | "item" (наименование+марка) | "system_item".
        system / query / code: те же фильтры, что в spec_rows.
    Строки с нечисловым количеством перечислены по rid — их считай вручную.
    """
    res = await _spec(project)
    rows = filter_rows(res, code, "", "", system, query)
    return f"строк {len(rows)}\n" + totals_md(rows, by if by in ("system", "item", "system_item")
                                               else "system")


def _vision_meta(sh: Sheet, tiles, notes) -> str:
    b = [f"Лист {sh.sid} (страница PDF {sh.page + 1}) · шифр {sh.code or '—'} · "
         f"лист по штампу {sh.sheet_no or '—'} · {sh.name()} · {sh.fmt} · тип {sh.kind}"]
    if notes:
        b.append("нарезка: " + "; ".join(notes))
    b.append("Кадры и текстовый слой PDF в каждом кадре (слой может быть неполным):")
    for t in tiles:
        b.append(f"- {t.label} [x {t.nbox[0]:.2f}–{t.nbox[2]:.2f}, y {t.nbox[1]:.2f}–{t.nbox[3]:.2f}]: "
                 f"{_s(t.text, 500) or '(текстового слоя нет)'}")
    return "\n".join(b)


@mcp.tool()
async def look(project: str, sid: str, question: str, box: str = "",
               zoom: float = 2.0) -> str:
    """
    Зрение: лист режется на кадры высокой чёткости, визуальная модель отвечает на
    вопрос ТЕКСТОМ с привязкой к кадрам. Для функции изделия по схеме, исполнения
    клапанов, трасс, узлов, сканов. Вопрос — узкий и конкретный:
    «система ДВ1: клапаны — марка, НО/НЗ, EI, что обслуживают».
    Ответы кэшируются (тот же вопрос — мгновенно).
    Args:
        sid: лист, например "L29".
        box: область "x0,y0,x1,y1" в долях листа — приблизить (из ответа look или кадров).
        zoom: кратность детализации для box (2 = вдвое чётче стандарта).
    """
    pr = _project(project)
    sh = _sheet(pr, sid)
    q = (question or "").strip()
    if len(q) < 8:
        raise ValueError("вопрос слишком общий: назови систему, изделия и что нужно установить")
    b = _parse_box(box) if box else None
    zoom = max(1.0, min(float(zoom or 2.0), 4.0))
    ck = _k("look", project, sh.sid,
            sha16(f"{vision.MODEL}|{b}|{zoom}|{q}".encode()) + ".txt")
    cached = store.load_text(ck)
    if cached:
        return cached + "\n(из кэша)"

    images: List[Tuple[str, bytes]] = []
    notes: List[str] = []
    if b is None:
        sr = await asyncio.to_thread(_render_sync, project, sh)
        tiles = list(sr.tiles)
        notes = list(sr.notes)
        if len(tiles) > 1:
            images.append((f"ОБЗОР всего листа {sh.sid} — только для ориентации", sr.overview))
    else:
        tiles = list(await asyncio.to_thread(_region_sync, project, sh, b, zoom))
    if not tiles:
        raise ValueError("область пуста или слишком мала")
    extra = ""
    if len(tiles) > LOOK_MAX_TILES:
        keep = {t.label for t in sorted(tiles, key=lambda t: -t.ink)[:LOOK_MAX_TILES]}
        extra = (f"! кадров {len(tiles)}, отправлены {LOOK_MAX_TILES} самых заполненных; "
                 f"пропущены: {', '.join(t.label for t in tiles if t.label not in keep)} — через box")
        tiles = [t for t in tiles if t.label in keep]
    for t in tiles:
        images.append((f"КАДР {t.label}: x {t.nbox[0]:.2f}–{t.nbox[2]:.2f}, "
                       f"y {t.nbox[1]:.2f}–{t.nbox[3]:.2f} листа", t.jpeg))

    ans = await vision.ask(q, _vision_meta(sh, tiles, notes), images)
    if ans.startswith("ОШИБКА"):
        return ans
    head = [f"look {sh.sid} · стр. PDF {sh.page + 1} · {sh.code or '—'} · "
            f"лист {sh.sheet_no or '—'} · {_s(sh.name(), 80)}" + (f" · box {b} ×{zoom}" if b else ""),
            "кадры: " + " ".join(f"{t.label}[{t.nbox[0]:.2f},{t.nbox[1]:.2f},{t.nbox[2]:.2f},"
                                 f"{t.nbox[3]:.2f}]" for t in tiles)]
    if extra:
        head.append(extra)
    head.append("--- ответ визуальной модели (пересказ изображения, не текстовый слой) ---")
    out = "\n".join(head) + "\n" + ans
    store.save_text(ck, out)
    return out


# ======================================================================
class TokenGuard:
    """Bearer-токен на всё, кроме /health (у сервера админский ключ OWUI)."""

    def __init__(self, app, token: str):
        self.app, self.token = app, token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and self.token and scope.get("path") != "/health":
            auth = dict(scope.get("headers") or []).get(b"authorization", b"").decode()
            if auth != f"Bearer {self.token}":
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


async def _health(_request):
    return JSONResponse({"status": "ok", "vision_model": vision.MODEL})


if __name__ == "__main__":
    port = int(os.getenv("MCP_SERVER_PORT", "8000"))
    app = mcp.streamable_http_app()
    app.router.routes.append(Route("/health", _health))
    token = os.getenv("MCP_TOKEN", "")
    if not token:
        log.warning("MCP_TOKEN не задан — сервер открыт всем в сети ai_network")
    log.info("Starting MCP DWG Server on :%d (streamable HTTP, /mcp)", port)
    uvicorn.run(TokenGuard(app, token), host="0.0.0.0", port=port, workers=1)
