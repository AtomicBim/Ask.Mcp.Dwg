"""Общие фикстуры: путь к src в sys.path и PDF для сравнения параллельного и последовательного разбора."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "mcp_server", "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


def _cyr_font():
    for p in (r"C:\Windows\Fonts\arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "/usr/share/fonts/dejavu/DejaVuSans.ttf", "/Library/Fonts/Arial.ttf"):
        if os.path.exists(p):
            return p
    return None


HDR = ["Поз.", "Наименование", "Тип, марка", "Код", "Завод", "Ед.", "Кол.", "Масса", "Прим."]
ROWS = [["1", "Вентилятор канальный", "ВК-100", "", "Завод", "шт", "2", "10", ""],
        ["2", "Клапан огнезадерживающий", "КПД-4", "", "Завод", "шт", "5", "3", ""],
        ["3", "Воздуховод", "d200", "", "", "м", "12", "", ""]]


def _text(page, xy, s, size):
    page.insert_text(xy, s, fontsize=size, fontname="cyr", fontfile=_cyr_font())


def _synthetic_pdf(path: str) -> str:
    """Два листа СО (линейная таблица, система через границу листа) + пустой бланк."""
    import pymupdf as fitz

    if _cyr_font() is None:
        pytest.skip("нет TTF с кириллицей для синтетического PDF (задай DWG_SAMPLE_PDF)")
    doc = fitz.open()
    x0, y0, cw, rh = 40, 40, 80, 22
    for n in (1, 2):
        page = doc.new_page(width=842, height=595)
        data = [HDR] + ([["", "Система П1", "", "", "", "", "", "", ""]] if n == 1 else []) + ROWS
        for i, r in enumerate(data):
            for j, v in enumerate(r):
                rect = fitz.Rect(x0 + j * cw, y0 + i * rh, x0 + (j + 1) * cw, y0 + (i + 1) * rh)
                page.draw_rect(rect, color=(0, 0, 0), width=0.6)
                if v:
                    _text(page, (rect.x0 + 2, rect.y0 + 14), v, 6.5)
        # штамп в правом нижнем углу: шифр и графы «Стадия / Лист / Листов»
        _text(page, (640, 540), "АТОМ-055-ОВ.СО", 10)
        _text(page, (640, 556), "Стадия", 6)
        _text(page, (690, 556), "Лист", 6)
        _text(page, (740, 556), "Листов", 6)
        _text(page, (645, 570), "П", 8)
        _text(page, (695, 570), str(n), 8)
        _text(page, (745, 570), "2", 8)
        _text(page, (40, 570), "Спецификация оборудования, изделий и материалов", 9)
    page = doc.new_page(width=842, height=595)              # пустой бланк СО: сетка без слов
    for i in range(12):
        for j in range(9):
            page.draw_rect(fitz.Rect(40 + j * 80, 40 + i * 22, 120 + j * 80, 62 + i * 22),
                           color=(0, 0, 0), width=0.6)
    _text(page, (640, 540), "АТОМ-055-ОВ.СО", 10)
    _text(page, (640, 556), "Стадия", 6)
    _text(page, (690, 556), "Лист", 6)
    _text(page, (740, 556), "Листов", 6)
    _text(page, (645, 570), "П", 8)
    _text(page, (695, 570), "3", 8)
    _text(page, (745, 570), "3", 8)
    _text(page, (40, 570), "Спецификация оборудования, изделий и материалов", 9)
    doc.save(path)
    doc.close()
    return path


@pytest.fixture(scope="session")
def sample_pdf(tmp_path_factory):
    env = os.getenv("DWG_SAMPLE_PDF")
    if env:
        if not os.path.exists(env):
            pytest.skip(f"DWG_SAMPLE_PDF не найден: {env}")
        return env
    pytest.importorskip("pymupdf")
    return _synthetic_pdf(str(tmp_path_factory.mktemp("pdf") / "sample.pdf"))
