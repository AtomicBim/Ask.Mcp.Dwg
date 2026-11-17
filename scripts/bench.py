"""
Нагрузочная проверка: open_project-эквивалент + spec на эталонном PDF, параллельно
20 запросов tools/list к живому серверу каждые 100 мс.

    python scripts/bench.py путь\\к\\эталону.pdf [--url http://127.0.0.1:8002/mcp] [--token T]

Ingest/spec гоняются напрямую через workers (без OWUI), а ListTools — HTTP-запросами
к уже запущенному серверу (если --url не задан, замер ListTools пропускается).
Цели: p95 ListTools < 200 мс, повторный вызов < 100 мс, общее время <= 2 мин.
"""
import argparse
import asyncio
import os
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "mcp_server", "src"))
os.environ.setdefault("DATA_DIR", os.path.join(ROOT, "data"))

import httpx  # noqa: E402

import ingest  # noqa: E402
import spec  # noqa: E402
import workers  # noqa: E402


async def list_tools_probe(url: str, token: str, n: int, period: float, out: list) -> None:
    hdr = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token:
        hdr["Authorization"] = f"Bearer {token}"
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    async with httpx.AsyncClient(timeout=30, trust_env=False) as c:
        for _ in range(n):
            t0 = time.monotonic()
            try:
                r = await c.post(url, headers=hdr, json=body)
                r.raise_for_status()
                out.append((time.monotonic() - t0) * 1000)
            except Exception as e:                                  # noqa: BLE001
                out.append(float("inf"))
                print("tools/list:", repr(e))
            await asyncio.sleep(period)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--url", default="")
    ap.add_argument("--token", default=os.getenv("MCP_TOKEN", ""))
    a = ap.parse_args()

    name = os.path.basename(a.pdf)
    data = open(a.pdf, "rb").read()
    sha = ingest.sha16(data)
    lat: list = []
    probe = None
    if a.url:
        probe = asyncio.create_task(list_tools_probe(a.url, a.token, 20 * 60, 0.1, lat))

    t0 = time.monotonic()
    pr = await workers.build_project_parallel(a.pdf, name, sha)
    t_ing = time.monotonic() - t0
    print(f"ingest: {len(pr.sheets)} листов за {t_ing:.1f} с ({workers.MAX_WORKERS} воркеров)")

    t1 = time.monotonic()
    res = await workers.build_spec_parallel(pr, a.pdf)
    t_spec = time.monotonic() - t1
    print(f"spec: {len(res.rows)} строк за {t_spec:.1f} с; отчёт: "
          f"{ {k: len(v) for k, v in res.report.items()} }")

    t2 = time.monotonic()
    await workers.build_spec_parallel(pr, a.pdf)
    print(f"повторная сборка (без кэша диска, для справки): {time.monotonic() - t2:.1f} с")

    total = time.monotonic() - t0
    print(f"ИТОГО: {total:.1f} с (цель <= 120)")
    if probe:
        probe.cancel()
        ok = sorted(x for x in lat if x != float("inf"))
        if ok:
            p95 = ok[min(len(ok) - 1, int(len(ok) * 0.95))]
            print(f"tools/list: n={len(ok)} med={statistics.median(ok):.0f} мс "
                  f"p95={p95:.0f} мс (цель < 200); ошибок {len(lat) - len(ok)}")
    workers.shutdown()
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    raise SystemExit(asyncio.run(main()))
