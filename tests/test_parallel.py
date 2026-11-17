"""Параллельные ingest/spec должны совпадать с последовательными (эталон)."""
import asyncio
import os

import pytest

import ingest
import spec
import workers
from ingest import SourcePdf


def _sheet_key(p):
    return [(s.sid, s.kind, s.code, s.sheet_no, s.page, s.title, s.is_scan) for s in p.sheets]


def _row_key(res):
    return [(r.rid, r.code, r.so_sheet, r.pdf_page, r.sid, r.system, r.group, r.pos, r.name,
             r.mark, r.art, r.maker, r.unit, r.qty, r.mass, r.note, r.wrapped, tuple(r.flags))
            for r in res.rows]


@pytest.fixture(scope="module")
def seq_project(sample_pdf):
    data = open(sample_pdf, "rb").read()
    return ingest.build_project([SourcePdf(os.path.basename(sample_pdf), data)]), data


def test_chunks_cover_all_pages_in_order():
    for n in (1, 2, 7, 64):
        for k in (1, 3, 8, 100):
            flat = [p for c in workers._chunks(n, k) for p in c]
            assert flat == list(range(n))


def test_build_project_parallel_matches_sequential(sample_pdf, seq_project):
    pr_seq, data = seq_project
    sha = ingest.sha16(data)
    pr_par = asyncio.run(workers.build_project_parallel(sample_pdf, os.path.basename(sample_pdf), sha))
    assert pr_par.pid == pr_seq.pid
    assert _sheet_key(pr_par) == _sheet_key(pr_seq)
    assert pr_par.warnings == pr_seq.warnings


def test_spec_parallel_matches_sequential(sample_pdf, seq_project):
    pr_seq, data = seq_project
    res_seq = spec.build_spec(pr_seq, data)

    async def go():
        return await workers.build_spec_parallel(pr_seq, sample_pdf)
    res_par = asyncio.run(go())
    assert _row_key(res_par) == _row_key(res_seq)
    assert res_par.docs == res_seq.docs
    assert res_par.systems == res_seq.systems
    assert res_par.report == res_seq.report


def test_prefilter_skips_empty_grid(sample_pdf):
    if os.getenv("DWG_SAMPLE_PDF"):
        pytest.skip("синтетический бланк есть только в сгенерированном PDF")
    import pymupdf as fitz
    doc = fitz.open(sample_pdf)
    try:
        tabs, skipped = spec._page_tables(doc[len(doc) - 1])
        assert skipped and tabs == []
        tabs, skipped = spec._page_tables(doc[0])
        assert not skipped
    finally:
        doc.close()
