"""Hard filters for raw rows before normalization."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

_LINK_CACHE: dict[str, tuple[datetime, bool]] = {}
_LINK_TTL = timedelta(hours=24)
_VAGUE_NAMES = {
    "sneaker",
    "chaussure",
    "shoes",
    "basket",
    "modele",
    "unknown",
    "n/a",
}

_PARASITIC_WORDS: frozenset[str] = frozenset({
    "kids", "junior", "enfant", "used", "occasion", "ebay", "vinted", "fake",
})


def _to_dt(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    text = str(raw or "").strip()
    if not text:
        return datetime.now(timezone.utc) - timedelta(days=365)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc) - timedelta(days=365)


def _is_live_url(url: str) -> bool:
    now = datetime.now(timezone.utc)
    cached = _LINK_CACHE.get(url)
    if cached and (now - cached[0]) <= _LINK_TTL:
        return cached[1]
    ok = False
    try:
        response = requests.head(url, timeout=4, allow_redirects=True)
        ok = response.status_code != 404
    except requests.RequestException:
        ok = False
    _LINK_CACHE[url] = (now, ok)
    return ok


def is_row_valid(row: dict[str, Any]) -> tuple[bool, str | None]:
    raw_price = row.get("price")
    if raw_price is None or str(raw_price).strip() == "":
        return False, "price_invalid"
    try:
        price = float(str(raw_price).replace(",", "."))
    except ValueError:
        return False, "price_invalid"
    if price < 20 or price > 5000:
        return False, "price_absurd"

    url = str(row.get("url") or "").strip()
    skip_link_check = bool(row.get("skip_link_check")) or (os.getenv("V2_SKIP_HTTP_CHECK", "0").strip() == "1")
    if not url:
        return False, "dead_link"
    if not url.startswith("http"):
        return False, "dead_link"
    if not skip_link_check and not _is_live_url(url):
        return False, "dead_link"

    brand = str(row.get("brand") or "").strip()
    if not brand:
        return False, "no_brand"

    name = str(row.get("name") or row.get("model") or "").strip()
    if len(name) < 5 or name.lower() in _VAGUE_NAMES:
        return False, "name_too_vague"
    if any(w in name.lower() for w in _PARASITIC_WORDS):
        return False, "parasitic_word"

    timestamp = _to_dt(row.get("timestamp") or row.get("updated_at"))
    if (datetime.now(timezone.utc) - timestamp) > timedelta(days=30):
        return False, "row_too_old"

    currency = str(row.get("currency") or "").strip().lower()
    if currency not in {"eur", "€", "euro"}:
        return False, "currency_unknown"

    return True, None
