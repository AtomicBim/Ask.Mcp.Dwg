"""owui — поиск и скачивание PDF, загруженных в чат OpenWebUI (админский API-ключ)."""
from __future__ import annotations

import os
import re
from typing import Dict, List, Optional, Tuple

import httpx

OWUI_URL = os.getenv("OWUI_URL", "http://open-webui:8080").rstrip("/")
OWUI_API_KEY = os.getenv("OWUI_API_KEY", "")


def _h() -> Dict[str, str]:
    return {"Authorization": f"Bearer {OWUI_API_KEY}"}


def _norm(s: str) -> str:
    s = (s or "").lower().replace("ё", "е")
    s = re.sub(r"\.pdf$", "", s.strip())
    return re.sub(r"\s+", " ", s)


async def list_pdfs(user_id: str = "") -> List[dict]:
    async with httpx.AsyncClient(timeout=60, trust_env=False) as c:   # внутренняя сеть, без VPN
        r = await c.get(f"{OWUI_URL}/api/v1/files/", headers=_h(), params={"content": "false"})
        r.raise_for_status()
        data = r.json()
    items = data.get("items", []) if isinstance(data, dict) else (data or [])
    out = []
    for f in items:
        meta = f.get("meta") or {}
        name = f.get("filename") or meta.get("name") or ""
        ctype = (meta.get("content_type") or "").lower()
        if not (name.lower().endswith(".pdf") or "pdf" in ctype):
            continue
        if user_id and f.get("user_id") and f["user_id"] != user_id:
            continue
        out.append({"id": f["id"], "name": name, "size": int(meta.get("size") or 0),
                    "created_at": int(f.get("created_at") or 0), "user_id": f.get("user_id", "")})
    out.sort(key=lambda x: -x["created_at"])
    return out


def pick(files: List[dict], q: str) -> Tuple[Optional[dict], List[dict]]:
    q = (q or "").strip()
    if not q:
        return (files[0] if files else None), files[1:6]
    for f in files:
        if f["id"] == q:
            return f, []
    nq = _norm(q)
    exact = [f for f in files if _norm(f["name"]) == nq]
    if exact:
        return exact[0], exact[1:5]
    part = [f for f in files if nq in _norm(f["name"])]
    return (part[0] if part else None), part[1:6]


async def download(file_id: str) -> bytes:
    async with httpx.AsyncClient(timeout=300, trust_env=False, follow_redirects=True) as c:
        r = await c.get(f"{OWUI_URL}/api/v1/files/{file_id}/content", headers=_h())
        r.raise_for_status()
        return r.content
