"""Cache discret des derniers prix valides par source / modèle (repli non bloquant)."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

_CACHE_PATH = Path(__file__).resolve().parents[1] / "data" / "last_good_scrape_cache.json"
_lock = threading.Lock()


def _load_all() -> dict[str, Any]:
    if not _CACHE_PATH.is_file():
        return {}
    try:
        raw = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _key(source: str, brand: str, model: str) -> str:
    return f"{(source or '').strip()}|{(brand or '').strip()}|{(model or '').strip()}"


def get_last_good_prices(source: str, brand: str, model: str) -> list[float]:
    data = _load_all()
    entry = data.get(_key(source, brand, model))
    if not isinstance(entry, list):
        return []
    out: list[float] = []
    for x in entry:
        try:
            v = float(x)
            if v > 0:
                out.append(v)
        except (TypeError, ValueError):
            continue
    return out[:60]


def save_last_good_prices(source: str, brand: str, model: str, prices: list[float]) -> None:
    if not prices:
        return
    key = _key(source, brand, model)
    try:
        serial = [round(float(p), 2) for p in prices[:60] if float(p) > 0]
    except (TypeError, ValueError):
        return
    if not serial:
        return
    with _lock:
        data = _load_all()
        data[key] = serial
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
