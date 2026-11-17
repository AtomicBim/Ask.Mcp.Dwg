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
import contextvars
import functools
import inspect
import json
import logging
import os
import re
import time
import typing
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import uvicorn
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse
from starlette.routing import Route

import owui
import store
import vision
import workers
from dwg_l0 import SheetRender, sha16
from ingest import KIND_LIST, KIND_OD, KIND_SPEC, Project, Sheet, _s
from spec import SpecResult, filter_rows, rows_md, totals_md

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("mcp_dwg")

SCHEMA = "v2"                      # поменять, если изменились классы Sheet/SpecRow/отчёт СО
SPEC_WAIT_S = float(os.getenv("SPEC_WAIT_S", "100"))
LOOK_MAX_TILES = int(os.getenv("LOOK_MAX_TILES", "12"))
TEXT_MAX = int(os.getenv("SHEET_TEXT_MAX", "40000"))
ROWS_LIMIT = int(os.getenv("SPEC_ROWS_LIMIT", "250"))
REQUIRE_USER = os.getenv("REQUIRE_USER_HEADER", "false").lower() == "true"

mcp = FastMCP("DwgReviewServer", stateless_http=True, json_response=True,
              transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

LOOK_MANY_MAX = int(os.getenv("LOOK_MANY_MAX", "8"))
WARMUP = os.getenv("WARMUP", "true").lower() == "true"

_projects = store.Lru(6)
_specs = store.Lru(6)
_spec_tasks: Dict[str, asyncio.Task] = {}
_warm_tasks: Dict[str, asyncio.Task] = {}
_inflight: Dict[str, "asyncio.Future[Any]"] = {}


# ----------------------------------------------------------------------
def _k(*parts: str) -> str:
    return "/".join((SCHEMA,) + parts)


async def singleflight(key: str, factory: Callable[[], Awaitable[Any]]) -> Any:
    """Параллельные вызовы с одним ключом ждут один расчёт. Ошибка уходит всем
    ожидающим, запись удаляется — следующий вызов считает заново."""
    fut = _inflight.get(key)
    if fut is not None:
        return await asyncio.shield(fut)
    fut = asyncio.get_running_loop().create_future()
    _inflight[key] = fut
    try:
        res = await factory()
        fut.set_result(res)
        return res
    except BaseException as e:
        fut.set_exception(e)
        fut.exception()                    # пометить прочитанным: ждущих может не быть
        raise
    finally:
        _inflight.pop(key, None)


# ----------------------------------------------------------------------
# телеметрия: одна JSON-строка на вызов инструмента
# ----------------------------------------------------------------------
_tele: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar(
    "tele", default=None)


def tele_add(**kv: Any) -> None:
    """Накопить поля текущего вызова (числа суммируются, прочее перезаписывается)."""
    t = _tele.get()
    if t is None:
        return
    for k, v in kv.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            t[k] = v
        else:
            t[k] = t.get(k, 0) + v


async def timed_call(label: str, coro: Awaitable[Any], key: str = "ms_pool") -> Any:
    """Замерить участок и добавить его длительность в поле key телеметрии."""
    t0 = time.monotonic()
    try:
        return await coro
    finally:
        tele_add(**{key: int((time.monotonic() - t0) * 1000)})


def timed(fn):
    """Декоратор инструмента: tool, project, sids, ms_total, ms_pool, ms_llm,
    cache_hit, tokens_in, tokens_out, model — одной строкой JSON в лог."""
    @functools.wraps(fn)
    async def wrap(*a, **kw):
        t: Dict[str, Any] = {"tool": fn.__name__, "ms_pool": 0, "ms_llm": 0,
                             "cache_hit": False, "tokens_in": 0, "tokens_out": 0}
        for name in ("project", "sids", "sid"):
            if name in kw:
                t[name] = kw[name]
        tok = _tele.set(t)
        t0 = time.monotonic()
        try:
            return await fn(*a, **kw)
        except Exception as e:
            t["error"] = repr(e)[:200]
            raise
        finally:
            t["ms_total"] = int((time.monotonic() - t0) * 1000)
            _tele.reset(tok)
            log.info("tool %s", json.dumps(t, ensure_ascii=False, default=str))
    # аннотации — строки (from __future__): отдаём mcp уже разрешённую сигнатуру
    hints = typing.get_type_hints(fn, include_extras=True)
    sig = inspect.signature(fn)
    wrap.__signature__ = sig.replace(
        parameters=[p.replace(annotation=hints.get(p.name, p.annotation))
                    for p in sig.parameters.values()],
        return_annotation=hints.get("return", sig.return_annotation))
    return wrap


async def _io(fn, *a):
    """Файловый I/O и pickle — вне event loop (GIL при чтении/записи отпускается)."""
    return await asyncio.to_thread(fn, *a)


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


def _pdf_path(pid: str) -> str:
    """Путь к PDF в кэше (воркеры открывают файл сами, bytes не гоняем)."""
    rel = _k("pdf", f"{pid}.pdf")
    if not store.exists(rel):
        raise ValueError(f"PDF проекта {pid!r} не найден в кэше: вызови open_project повторно")
    return store.path(rel)


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
# тяжёлые операции: PDF-работа в пуле процессов, кэш на диске, singleflight
# ----------------------------------------------------------------------
async def _spec_async(pid: str) -> SpecResult:
    async def go() -> SpecResult:
        pr = await _io(_project, pid)
        path = _pdf_path(pid)
        res = await timed_call("spec", workers.build_spec_parallel(pr, path))
        await _io(store.save_obj, _k("spec", f"{pid}.pkl"), res)
        log.info("spec %s: %d строк за %s с", pid, len(res.rows), res.seconds)
        return res
    return await singleflight(_k("spec", pid), go)


async def _render(pid: str, sh: Sheet, background: bool = False) -> SheetRender:
    key = _k("render", pid, f"{sh.sid}.pkl")

    async def go() -> SheetRender:
        sr = await _io(store.load_obj, key)
        if sr is None:
            path = _pdf_path(pid)
            sr = await timed_call("render", workers.run(
                workers.w_render_sheet, path, sh.page, sh.sid, background=background))
            await _io(store.save_obj, key, sr)
        return sr
    return await singleflight(key, go)


async def _region(pid: str, sh: Sheet, box, zoom: float):
    key = _k("region", pid, sh.sid, sha16(f"{box}|{zoom}".encode()))
    path = _pdf_path(pid)
    return await singleflight(key, lambda: timed_call("region", workers.run(
        workers.w_render_region, path, sh.page, tuple(box), sh.sid, zoom)))


async def _tables(pid: str, sh: Sheet, background: bool = False) -> List[str]:
    key = _k("tables", pid, f"{sh.sid}.pkl")

    async def go() -> List[str]:
        t = await _io(store.load_obj, key)
        if t is None:
            path = _pdf_path(pid)
            t = await timed_call("tables", workers.run(
                workers.w_sheet_tables, path, sh.page, background=background))
            await _io(store.save_obj, key, t)
        return t
    return await singleflight(key, go)


def _spec_start(pid: str) -> None:
    if _specs.get(pid) is not None or store.exists(_k("spec", f"{pid}.pkl")):
        return
    t = _spec_tasks.get(pid)
    if t is None or (t.done() and (t.cancelled() or t.exception() is not None)):
        _spec_tasks[pid] = asyncio.create_task(_spec_async(pid))


async def _warmup(pid: str) -> None:
    """Низкий приоритет (1 слот пула): таблицы листов ОД/ведомостей, чтобы
    первый sheet_text по ним был мгновенным."""
    try:
        pr = await _io(_project, pid)
        for sh in pr.sheets:
            if sh.is_scan or sh.kind not in (KIND_OD, KIND_LIST):
                continue
            await _tables(pid, sh, background=True)
        log.info("warmup %s: готово", pid)
    except Exception as e:
        log.warning("warmup %s: %r", pid, e)


def _warm_start(pid: str) -> None:
    if not WARMUP:
        return
    t = _warm_tasks.get(pid)
    if t is None or t.done():
        _warm_tasks[pid] = asyncio.create_task(_warmup(pid))


async def _spec(pid: str) -> SpecResult:
    res = _specs.get(pid) or await _io(store.load_obj, _k("spec", f"{pid}.pkl"))
    if res is not None:
        _specs.put(pid, res)
        tele_add(cache_hit=True)
        return res
    await _io(_project, pid)
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


async def _ingest(name: str, data: bytes, sha: str, pid: str) -> None:
    """Разбор нового PDF: сохранить файл, разобрать страницы в пуле, сохранить проект."""
    async def go() -> None:
        if await _io(store.exists, _k("projects", f"{pid}.pkl")):
            return
        await _io(store.save_bytes, _k("pdf", f"{pid}.pdf"), data)
        pr = await timed_call("ingest", workers.build_project_parallel(
            store.path(_k("pdf", f"{pid}.pdf")), name, sha))
        await _io(store.save_obj, _k("projects", f"{pid}.pkl"), pr)
        _projects.put(pid, pr)
    await singleflight(_k("open", pid), go)


@mcp.tool()
@timed
async def open_project(ctx: Context, file: str = "") -> str:
    """
    Открывает PDF из чата: разбор всех страниц (штамп, текстовый слой, тип листа),
    кэш по хэшу файла. Первый раз ~15–40 с, повторно мгновенно. В фоне запускает
    сборку строк спецификации для spec_rows.
    Возвращает project (pid) — передавай его во все остальные инструменты.
    Для нескольких листов используй look_many и диапазоны в sheet_text ("L3-L15");
    не делегируй обход листов субагентам — всё параллелится внутри сервера.
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
    pid = await _io(store.load_text, _k("fileid", f"{f['id']}.txt"))
    if not pid or not await _io(store.exists, _k("projects", f"{pid}.pkl")):
        data = await owui.download(f["id"])
        sha = sha16(data)
        pid = sha16(sha.encode())                     # как Project.pid в ingest для 1 файла
        await _ingest(f["name"], data, sha, pid)
        await _io(store.save_text, _k("fileid", f"{f['id']}.txt"), pid)
    else:
        tele_add(cache_hit=True)
    tele_add(project=pid)
    pr = await _io(_project, pid)
    _spec_start(pid)
    _warm_start(pid)

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
@timed
async def project_map(project: str) -> str:
    """
    Карта документа: sid (=страница PDF) | тип листа | раздел | шифр | лист по штампу |
    формат | наименование | число кадров. Основа для п.2 проверки и таблицы охвата.
    """
    pr = await _io(_project, project)
    return "sid Ln = страница PDF n\n" + pr.toc_md()


@mcp.tool()
@timed
async def sheet_text(project: str, sids: str, tables: str = "auto",
                     max_chars: int = TEXT_MAX) -> str:
    """
    Текстовый слой листов: штамп, примечания, полный текст, таблицы в Markdown.
    Картинок нет. Для общих данных, ведомостей, примечаний на схемах.
    Несколько листов бери одним вызовом-диапазоном, таблицы считаются параллельно.
    Args:
        sids: "L6" | "L6,L9" | "L3-L15".
        tables: "auto" (таблицы для ОД/ведомостей/СО) | "yes" | "no".
        max_chars: ограничение объёма ответа.
    """
    pr = await _io(_project, project)
    shs = _sids(pr, sids)
    tele_add(sids=",".join(s.sid for s in shs[:40]))
    # недостающие таблицы всего диапазона считаем параллельно до сборки ответа
    need = [sh for sh in shs if not sh.is_scan and not sh.tables_md and
            (tables == "yes" or (tables == "auto" and sh.kind in (KIND_OD, KIND_LIST, KIND_SPEC)))]
    if need:
        res = await asyncio.gather(*[_tables(project, sh) for sh in need],
                                   return_exceptions=True)
        for sh, r in zip(need, res):
            if isinstance(r, BaseException):
                log.warning("tables %s: %r", sh.sid, r)
            else:
                sh.tables_md = r
    out: List[str] = []
    used = 0
    for sh in shs:
        if sh.is_scan:
            part = (f"# {sh.sid} · стр. PDF {sh.page + 1} · {sh.name()}\n"
                    "текстового слоя нет — читать только через look")
        else:
            part = f"(стр. PDF {sh.page + 1})\n" + sh.card(with_text=True)
        if used + len(part) > max_chars:
            out.append(f"\n… обрезано на {sh.sid}: запроси остальные листы отдельно")
            break
        out.append(part)
        used += len(part)
    return "\n\n---\n\n".join(out)


@mcp.tool()
@timed
async def find(project: str, query: str, kind: str = "", section: str = "",
               limit: int = 8) -> str:
    """
    Лексический поиск (BM25) по листам проекта: марки, системы, шифры, слова.
    Примеры: "ДВ1", "КПД-4 EI 150", "гильза", "ПНОК".
    Args:
        kind: фильтр типа листа: "чертёж" | "ОД" | "СО" | "узел" | "ведомость" | "скан".
        section: фильтр раздела, например "ОВ".
    """
    pr = await _io(_project, project)
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
@timed
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
@timed
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


def _check_question(question: str) -> str:
    q = (question or "").strip()
    if len(q) < 8:
        raise ValueError("вопрос слишком общий: назови систему, изделия и что нужно установить")
    return q


async def _look_one(project: str, sh: Sheet, q: str, b, zoom: float) -> str:
    """Один лист: кэш -> рендер в пуле -> vision. Общий код look и look_many:
    ключ кэша и формат ответа одинаковы, поэтому кэш общий."""
    ck = _k("look", project, sh.sid,
            sha16(f"{vision.MODEL}|{b}|{zoom}|{q}".encode()) + ".txt")

    async def go() -> str:
        return await _look_compute(project, sh, q, b, zoom, ck)
    cached = await _io(store.load_text, ck)
    if cached:
        tele_add(cache_hit=True)
        return cached + "\n(из кэша)"
    return await singleflight(ck, go)


async def _look_compute(project: str, sh: Sheet, q: str, b, zoom: float, ck: str) -> str:
    images: List[Tuple[str, bytes]] = []
    notes: List[str] = []
    if b is None:
        sr = await _render(project, sh)
        tiles = list(sr.tiles)
        notes = list(sr.notes)
        if len(tiles) > 1:
            images.append((f"ОБЗОР всего листа {sh.sid} — только для ориентации", sr.overview))
    else:
        tiles = list(await _region(project, sh, b, zoom))
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

    t0 = time.monotonic()
    ans, usage = await vision.ask_ex(q, _vision_meta(sh, tiles, notes), images)
    tele_add(ms_llm=int((time.monotonic() - t0) * 1000), model=vision.MODEL,
             tokens_in=usage.get("prompt_tokens", 0),
             tokens_out=usage.get("completion_tokens", 0))
    if ans.startswith("ОШИБКА"):
        return ans                                   # ошибки не кэшируем
    head = [f"look {sh.sid} · стр. PDF {sh.page + 1} · {sh.code or '—'} · "
            f"лист {sh.sheet_no or '—'} · {_s(sh.name(), 80)}" + (f" · box {b} ×{zoom}" if b else ""),
            "кадры: " + " ".join(f"{t.label}[{t.nbox[0]:.2f},{t.nbox[1]:.2f},{t.nbox[2]:.2f},"
                                 f"{t.nbox[3]:.2f}]" for t in tiles)]
    if extra:
        head.append(extra)
    head.append("--- ответ визуальной модели (пересказ изображения, не текстовый слой) ---")
    out = "\n".join(head) + "\n" + ans
    await _io(store.save_text, ck, out)
    return out


@mcp.tool()
@timed
async def look(project: str, sid: str, question: str, box: str = "",
               zoom: float = 2.0) -> str:
    """
    Зрение: лист режется на кадры высокой чёткости, визуальная модель отвечает на
    вопрос ТЕКСТОМ с привязкой к кадрам. Для функции изделия по схеме, исполнения
    клапанов, трасс, узлов, сканов. Вопрос — узкий и конкретный:
    «система ДВ1: клапаны — марка, НО/НЗ, EI, что обслуживают».
    Ответы кэшируются (тот же вопрос — мгновенно).
    Несколько листов с одним вопросом — одним вызовом look_many, не серией look.
    Args:
        sid: лист, например "L29".
        box: область "x0,y0,x1,y1" в долях листа — приблизить (из ответа look или кадров).
        zoom: кратность детализации для box (2 = вдвое чётче стандарта).
    """
    pr = await _io(_project, project)
    sh = _sheet(pr, sid)
    q = _check_question(question)
    b = _parse_box(box) if box else None
    zoom = max(1.0, min(float(zoom or 2.0), 4.0))
    return await _look_one(project, sh, q, b, zoom)


@mcp.tool()
@timed
async def look_many(project: str, sids: str, question: str,
                    max_sheets: int = LOOK_MANY_MAX) -> str:
    """
    Зрение по нескольким листам сразу: рендер и вопросы к визуальной модели идут
    параллельно, каждый лист кэшируется как в look. Один и тот же вопрос задаётся
    каждому листу — формулируй его так, чтобы он подходил всем. Ошибка на одном
    листе не отменяет остальные. Не делегируй обход листов субагентам.
    Args:
        sids: "L29,L31" | "L29-L34".
        question: узкий конкретный вопрос (как в look).
        max_sheets: максимум листов за вызов (остальные — отдельным вызовом).
    """
    pr = await _io(_project, project)
    shs = _sids(pr, sids)
    q = _check_question(question)
    seen, uniq = set(), []
    for sh in shs:
        if sh.sid not in seen:
            seen.add(sh.sid)
            uniq.append(sh)
    cap = max(1, min(int(max_sheets or LOOK_MANY_MAX), 20))
    take, rest = uniq[:cap], uniq[cap:]
    tele_add(sids=",".join(s.sid for s in take))

    async def one(sh: Sheet) -> str:
        return await _look_one(project, sh, q, None, 2.0)

    res = await asyncio.gather(*[one(sh) for sh in take], return_exceptions=True)
    out: List[str] = []
    for sh, r in zip(take, res):
        if isinstance(r, BaseException):
            r = f"ОШИБКА: {r}"
        out.append(f"## {sh.sid}\n{r}")
    if rest:
        out.append(f"… не обработаны (лимит {cap}): {', '.join(s.sid for s in rest)} — "
                   "запроси отдельным вызовом")
    return "\n\n---\n\n".join(out)


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
