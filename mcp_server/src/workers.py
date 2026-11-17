"""
workers — пул процессов для всего, что вызывает PyMuPDF.

Зачем: find_tables/get_text — чистый Python под GIL, а PyMuPDF не потокобезопасен.
Отдельные процессы обходят GIL и заменяют глобальный FITZ-lock: PDF разных
пользователей и страницы одного файла обрабатываются параллельно.

Правила:
  * функции воркеров — только верхнего уровня (spawn, Windows), без замыканий;
  * PDF открывается по пути в DATA_DIR (bytes не пиклим), документ кэшируется
    в процессе (LRU на DOC_LRU штук);
  * наружу возвращаются только picklable-объекты (Sheet, SheetRender, ячейки).
"""
from __future__ import annotations

import asyncio
import atexit
import logging
import multiprocessing as mp
import os
import time
from collections import OrderedDict
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import pymupdf as fitz

import dwg_l0
import ingest
import spec as spec_mod
from ingest import Project, Sheet, SourcePdf

log = logging.getLogger("workers")

MAX_WORKERS = max(1, int(os.getenv("INGEST_WORKERS", "") or max(1, (os.cpu_count() or 2) - 1)))
DOC_LRU = int(os.getenv("WORKER_DOC_LRU", "2"))
BG_SLOTS = int(os.getenv("WARMUP_SLOTS", "1"))

# ----------------------------------------------------------------------
# сторона воркера
# ----------------------------------------------------------------------
_docs: "OrderedDict[str, Any]" = OrderedDict()


def _doc(path: str):
    """Открытый документ по пути, LRU в процессе воркера."""
    d = _docs.get(path)
    if d is not None:
        _docs.move_to_end(path)
        return d
    d = fitz.open(path)
    _docs[path] = d
    while len(_docs) > max(1, DOC_LRU):
        _, old = _docs.popitem(last=False)
        try:
            old.close()
        except Exception:
            pass
    return d


def _init_worker() -> None:
    import logging as _l
    _l.getLogger("pymupdf").setLevel(_l.ERROR)
    try:
        fitz.TOOLS.mupdf_display_errors(False)
    except Exception:
        pass


def w_build_pages(path: str, name: str, sha: str, pages: List[int]) -> List[Sheet]:
    return ingest.build_sheets(_doc(path), name, sha, pages)


def w_page_tables(path: str, page: int) -> Tuple[List[List[list]], bool]:
    return spec_mod.page_tables_cells(_doc(path), page)


def w_sheet_tables(path: str, page: int) -> List[str]:
    return ingest.page_tables_md(_doc(path), page)


def w_render_sheet(path: str, page: int, sid: str) -> dwg_l0.SheetRender:
    return dwg_l0.render_sheet_doc(_doc(path), page, sid)


def w_render_region(path: str, page: int, box: Tuple[float, float, float, float],
                    sid: str, zoom: float):
    return dwg_l0.render_region_doc(_doc(path), page, box, sid, zoom=zoom)


def w_ping() -> int:
    return os.getpid()


# ----------------------------------------------------------------------
# сторона сервера
# ----------------------------------------------------------------------
_pool: Optional[ProcessPoolExecutor] = None
_sem: Optional[asyncio.Semaphore] = None
_bg_sem: Optional[asyncio.Semaphore] = None
_sem_loop: Optional[asyncio.AbstractEventLoop] = None


def _new_pool() -> ProcessPoolExecutor:
    return ProcessPoolExecutor(max_workers=MAX_WORKERS, mp_context=mp.get_context("spawn"),
                               initializer=_init_worker)


def pool() -> ProcessPoolExecutor:
    global _pool
    if _pool is None:
        _pool = _new_pool()
        log.info("пул процессов: %d воркеров", MAX_WORKERS)
    return _pool


def shutdown() -> None:
    global _pool
    if _pool is not None:
        _pool.shutdown(wait=False, cancel_futures=True)
        _pool = None


atexit.register(shutdown)


def _sems() -> Tuple[asyncio.Semaphore, asyncio.Semaphore]:
    """Семафоры привязаны к event loop: создаём лениво внутри работающего loop."""
    global _sem, _bg_sem, _sem_loop
    loop = asyncio.get_running_loop()
    if _sem is None or _sem_loop is not loop:
        _sem, _bg_sem, _sem_loop = (asyncio.Semaphore(MAX_WORKERS),
                                    asyncio.Semaphore(max(1, BG_SLOTS)), loop)
    return _sem, _bg_sem


async def run(fn: Callable, *args, background: bool = False) -> Any:
    """Выполнить fn в пуле. background=True — низкий приоритет: не более BG_SLOTS
    фоновых задач одновременно (прогрев не должен занимать всех воркеров).
    При BrokenProcessPool (segfault MuPDF) пул пересоздаётся, вызов повторяется 1 раз."""
    global _pool
    sem, bg = _sems()
    loop = asyncio.get_running_loop()
    for attempt in (0, 1):
        try:
            if background:
                async with bg:
                    async with sem:
                        return await loop.run_in_executor(pool(), fn, *args)
            async with sem:
                return await loop.run_in_executor(pool(), fn, *args)
        except BrokenProcessPool:
            log.error("пул процессов сломан (%s), пересоздаю", getattr(fn, "__name__", fn))
            shutdown()
            if attempt:
                raise
    raise RuntimeError("unreachable")


def _chunks(n: int, k: int) -> List[List[int]]:
    """Страницы 0..n-1 -> k чанков: соседние страницы вместе (локальность документа)."""
    k = max(1, min(k, n))
    size, extra = divmod(n, k)
    out, start = [], 0
    for i in range(k):
        end = start + size + (1 if i < extra else 0)
        out.append(list(range(start, end)))
        start = end
    return [c for c in out if c]


async def build_project_parallel(path: str, name: str, sha: str) -> Project:
    """Параллельный аналог ingest.build_project для одного файла в DATA_DIR."""
    with fitz.open(path) as d:
        total = len(d)
    parts = await asyncio.gather(*[
        run(w_build_pages, path, name, sha, ch) for ch in _chunks(total, MAX_WORKERS * 2)])
    sheets: List[Sheet] = [sh for part in parts for sh in part]
    src = SourcePdf(name=name, data=b"", sha=sha)
    return ingest.finalize_project([src], [sheets], [total])


async def build_spec_parallel(pr: Project, path: str):
    """Параллельный аналог spec.build_spec: таблицы листов СО — в пул, свёртка — здесь."""
    t0 = time.monotonic()
    shs = spec_mod.spec_sheets(pr)
    res = await asyncio.gather(*[run(w_page_tables, path, sh.page, background=True)
                                 for sh in shs])
    tabs = {sh.sid: r for sh, r in zip(shs, res)}
    return await asyncio.to_thread(spec_mod.assemble_spec, pr, tabs, t0)
