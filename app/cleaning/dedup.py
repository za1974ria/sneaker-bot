"""Near-duplicate row removal."""

from __future__ import annotations

from app.cleaning.normalizer import CleanRow


def dedupe(rows: list[CleanRow]) -> list[CleanRow]:
    seen: set[tuple[str, str, str | None, float, str]] = set()
    out: list[CleanRow] = []
    for row in rows:
        key = (row.brand.lower(), row.model_guess.lower(), row.ref, round(row.price_eur, 2), row.source.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out
