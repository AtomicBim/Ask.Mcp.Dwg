"""vision — вопрос к Gemini (OpenRouter) по кадрам листа. Ответ — текст."""
from __future__ import annotations

import asyncio
import base64
import logging
import os
from typing import List, Tuple

import httpx

log = logging.getLogger("vision")

OR_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = os.getenv("VISION_MODEL", "google/gemini-3.1-pro-preview")   # сверить slug в OpenRouter
KEY = os.getenv("OPENROUTER_API_KEY", "")
MAX_TOKENS = int(os.getenv("VISION_MAX_TOKENS", "12000"))
TIMEOUT = float(os.getenv("VISION_TIMEOUT", "300"))
_sem = asyncio.Semaphore(int(os.getenv("VISION_CONCURRENCY", "3")))

SYSTEM = """Ты — инженер, читающий чертежи раздела ОВ (отопление, вентиляция, противодымная вентиляция).
Тебе дают изображения ОДНОГО листа PDF (обзор и кадры высокой чёткости) и конкретный вопрос.
Задача — извлечь из изображения факты для ответа. Правила:
1. Только то, что видно. Не восстанавливай по типовым решениям, по другим листам, по смыслу.
2. Надписи, марки, размеры, отметки, обозначения систем, позиций и клапанов приводи буквально, в кавычках.
3. Каждый факт привязывай к кадру: «(кадр L29R2C3, верх-лево)». Мелкий текст читай по кадрам, не по обзору.
4. Место есть, но не читается — «не читается (кадр …)», без догадок. Нет на листе — «на листе не найдено».
5. Кадры перекрываются: объект на стыке может попасть в два кадра — не считай дважды; при подсчёте укажи кадры.
6. Текстовый слой PDF дан как подсказка. Если он расходится с изображением — укажи оба варианта.
7. Не делай нормативных выводов и не оценивай допустимость — только факты.
8. Если для точного ответа нужна детализация — укажи область листа в долях (x0,y0,x1,y1) для приближения.
Формат ответа:
## Факты
- …
## Не найдено / не читается
- …
## Приблизить (если нужно)
- x0,y0,x1,y1 — зачем"""


async def ask(question: str, meta: str, images: List[Tuple[str, bytes]]) -> str:
    if not KEY:
        return "ОШИБКА: OPENROUTER_API_KEY не задан"
    content = [{"type": "text", "text": f"{meta}\n\nВОПРОС:\n{question}\n\n"
                                        "Далее изображения; перед каждым — подпись кадра."}]
    for cap, jpg in images:
        content.append({"type": "text", "text": cap})
        content.append({"type": "image_url", "image_url": {
            "url": "data:image/jpeg;base64," + base64.b64encode(jpg).decode()}})
    body = {"model": MODEL, "max_tokens": MAX_TOKENS,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": content}]}
    headers = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json",
               "HTTP-Referer": os.getenv("OPENROUTER_SITE_URL", "http://mcp-dwg-server"),
               "X-Title": os.getenv("OPENROUTER_SITE_NAME", "Ask.Mcp.Dwg")}
    last = ""
    async with _sem:
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=TIMEOUT) as c:     # trust_env -> VPN-прокси
                    r = await c.post(OR_URL, headers=headers, json=body)
                if r.status_code in (408, 429, 500, 502, 503, 504):
                    last = f"HTTP {r.status_code}: {r.text[:300]}"
                    await asyncio.sleep(6 * (attempt + 1))
                    continue
                r.raise_for_status()
                msg = r.json()["choices"][0]["message"]
                txt = msg.get("content")
                if isinstance(txt, list):
                    txt = "".join(p.get("text", "") for p in txt if isinstance(p, dict))
                return (txt or "").strip() or "ОШИБКА: пустой ответ визуальной модели"
            except httpx.HTTPStatusError as e:
                return f"ОШИБКА визуальной модели: HTTP {e.response.status_code}: {e.response.text[:300]}"
            except (httpx.RequestError, KeyError, IndexError, ValueError) as e:
                last = repr(e)
                await asyncio.sleep(6 * (attempt + 1))
    return f"ОШИБКА визуальной модели после 3 попыток: {last}"
