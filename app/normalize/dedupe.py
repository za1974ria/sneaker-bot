"""Duplicate removal utilities."""

from __future__ import annotations

from typing import Any


def dedupe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, float, str, str]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = (
            str(row.get("product", "")).strip().lower(),
            float(row.get("price", 0.0)),
            str(row.get("source", "")).strip().lower(),
            str(row.get("country", "")).strip().upper(),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out

