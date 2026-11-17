"""singleflight, телеметрия, совместимость vision.ask."""
import asyncio
import json
import logging

import pytest

import main
import vision


def test_singleflight_runs_once():
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return calls

    async def go():
        return await asyncio.gather(*[main.singleflight("k", factory) for _ in range(5)])
    assert asyncio.run(go()) == [1] * 5
    assert calls == 1 and not main._inflight


def test_singleflight_error_propagates_and_resets():
    async def bad():
        await asyncio.sleep(0.01)
        raise RuntimeError("boom")

    async def go():
        r = await asyncio.gather(main.singleflight("e", bad), main.singleflight("e", bad),
                                 return_exceptions=True)
        assert all(isinstance(x, RuntimeError) for x in r)
        assert not main._inflight
        return await main.singleflight("e", lambda: asyncio.sleep(0, "ok"))
    assert asyncio.run(go()) == "ok"


def test_timed_logs_json(caplog):
    @main.timed
    async def demo(project: str = ""):
        main.tele_add(ms_llm=5, tokens_in=10, model="m")
        main.tele_add(ms_llm=5, tokens_in=1)
        return "x"

    caplog.set_level(logging.INFO, logger="mcp_dwg")
    assert asyncio.run(demo(project="p1")) == "x"
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("tool "))
    d = json.loads(line[5:])
    assert d["tool"] == "demo" and d["project"] == "p1"
    assert d["ms_llm"] == 10 and d["tokens_in"] == 11 and d["model"] == "m"
    assert "ms_total" in d


def test_ask_compat(monkeypatch):
    async def fake(q, m, i):
        return "ответ", {"prompt_tokens": 3}
    monkeypatch.setattr(vision, "ask_ex", fake)
    assert asyncio.run(vision.ask("q", "m", [])) == "ответ"
