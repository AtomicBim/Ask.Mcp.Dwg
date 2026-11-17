"""store — файловый кэш (все ключи производны от хэша PDF) + LRU в памяти."""
from __future__ import annotations

import os
import pickle
import threading
from collections import OrderedDict
from typing import Any, Optional

DATA = os.getenv("DATA_DIR", "/data")


def _p(rel: str) -> str:
    full = os.path.join(DATA, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    return full


def exists(rel: str) -> bool:
    return os.path.exists(os.path.join(DATA, rel))


def save_bytes(rel: str, b: bytes) -> None:
    full = _p(rel)
    tmp = full + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(b)
    os.replace(tmp, full)


def load_bytes(rel: str) -> Optional[bytes]:
    try:
        with open(os.path.join(DATA, rel), "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def save_obj(rel: str, obj: Any) -> None:
    save_bytes(rel, pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL))


def load_obj(rel: str) -> Any:
    b = load_bytes(rel)
    if b is None:
        return None
    try:
        return pickle.loads(b)
    except Exception:
        return None          # схема классов поменялась — пересоберём


def save_text(rel: str, s: str) -> None:
    save_bytes(rel, s.encode("utf-8"))


def load_text(rel: str) -> Optional[str]:
    b = load_bytes(rel)
    return b.decode("utf-8") if b is not None else None


class Lru:
    def __init__(self, cap: int = 6):
        self.cap, self.d, self.lock = cap, OrderedDict(), threading.Lock()

    def get(self, k):
        with self.lock:
            if k in self.d:
                self.d.move_to_end(k)
                return self.d[k]
            return None

    def put(self, k, v):
        with self.lock:
            self.d[k] = v
            self.d.move_to_end(k)
            while len(self.d) > self.cap:
                self.d.popitem(last=False)
