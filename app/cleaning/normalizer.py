"""Row normalization utilities for strict matching."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

BRAND_MAP = {
    "nike inc": "Nike",
    "nike": "Nike",
    "adidas originals": "Adidas",
    "adidas": "Adidas",
    "new balance": "New Balance",
    "puma": "Puma",
    "asics": "Asics",
}

CURRENCY_RATES_TO_EUR = {"EUR": 1.0, "USD": 0.92, "GBP": 1.17}
KNOWN_COLORS = ["WHITE", "BLACK", "RED", "BLUE", "GREEN", "GREY", "GRAY", "BEIGE", "PINK", "BROWN"]

NOISE_PATTERNS = [
    re.compile(r"[®™]"),
    re.compile(r"\bNEW\b", re.IGNORECASE),
    re.compile(r"\bSOLDES?\b", re.IGNORECASE),
    re.compile(r"\bPROMO\b", re.IGNORECASE),
    re.compile(r"-\d{1,2}%"),
]

SIZE_PATTERN = re.compile(r"\b(3[5-9]|4[0-8])(?:[.,]5)?\b")
NIKE_REF_PATTERN = re.compile(r"\b[A-Z]{2}\d{4}-\d{3}\b")
ADIDAS_REF_PATTERN = re.compile(r"\b[A-Z]{1,2}\d{5,6}\b")
NB_REF_PATTERN = re.compile(r"\b[A-Z]{1,3}\d{3,6}[A-Z]{0,3}\b")


@dataclass(frozen=True)
class CleanRow:
    name: str
    brand: str
    model_guess: str
    ref: str | None
    color: str | None
    size: str | None
    price_eur: float
    url: str
    source: str
    timestamp: datetime


def _parse_timestamp(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    text = str(raw or "").strip()
    if not text:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return datetime.now(timezone.utc)


def _normalize_name(name: str) -> str:
    result = (name or "").strip().lower()
    for pattern in NOISE_PATTERNS:
        result = pattern.sub(" ", result)
    return " ".join(result.split())


def _normalize_brand(brand: str) -> str:
    normalized = " ".join((brand or "").strip().lower().split())
    return BRAND_MAP.get(normalized, (brand or "").strip().title())


def _to_eur(price: Any, currency: str) -> float:
    amount = float(str(price or "0").replace(",", "."))
    fx = CURRENCY_RATES_TO_EUR.get((currency or "EUR").upper(), 1.0)
    return round(amount * fx, 2)


def _extract_size(text: str) -> str | None:
    match = SIZE_PATTERN.search(text)
    if not match:
        return None
    token = match.group(0).replace(",", ".")
    return token


def _extract_color(text: str) -> str | None:
    upper = text.upper()
    for color in KNOWN_COLORS:
        if color in upper:
            return color.title()
    return None


def _extract_ref(text: str) -> str | None:
    for pattern in (NIKE_REF_PATTERN, ADIDAS_REF_PATTERN, NB_REF_PATTERN):
        match = pattern.search(text.upper())
        if match:
            return match.group(0)
    return None


def normalize_row(row: dict[str, Any]) -> CleanRow:
    raw_name = str(row.get("name") or row.get("model") or "").strip()
    normalized_name = _normalize_name(raw_name)
    normalized_brand = _normalize_brand(str(row.get("brand") or ""))
    currency = str(row.get("currency") or "EUR").upper()
    combined_text = f"{raw_name} {row.get('description') or ''}"
    ref = _extract_ref(str(row.get("ref") or "")) or _extract_ref(combined_text)
    model_guess = normalized_name.replace(normalized_brand.lower(), "", 1).strip() or normalized_name
    return CleanRow(
        name=normalized_name,
        brand=normalized_brand,
        model_guess=model_guess,
        ref=ref,
        color=_extract_color(combined_text),
        size=_extract_size(combined_text),
        price_eur=_to_eur(row.get("price"), currency),
        url=str(row.get("url") or "").strip(),
        source=str(row.get("source") or row.get("shop") or "unknown").strip(),
        timestamp=_parse_timestamp(row.get("timestamp") or row.get("updated_at")),
    )
