"""Seed canonical_products from current sneaker catalog JSON."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "sneakerbot.db"
SNEAKERS_DB_PATH = ROOT / "static" / "sneakers_db.json"


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _guess_gender(model_name: str) -> str:
    lower = model_name.lower()
    if "women" in lower:
        return "W"
    if "kids" in lower or "junior" in lower:
        return "K"
    return "U"


def _guess_variant(model_name: str) -> str | None:
    if "platform" in model_name.lower():
        return "platform"
    return None


def seed_catalog() -> int:
    data = json.loads(SNEAKERS_DB_PATH.read_text(encoding="utf-8"))
    with sqlite3.connect(str(DB_PATH), timeout=20) as conn:
        conn.executescript((ROOT / "migrations" / "20260416_add_canonical_tables.sql").read_text(encoding="utf-8"))
        count = 0
        for model, meta in data.items():
            brand = str(meta.get("brand") or "").strip()
            ref = str(meta.get("ref") or "").strip()
            canonical_id = _slugify(f"{brand}-{model}-{ref or model}")
            aliases = [f"{brand} {model}".lower(), f"{model} {ref}".lower().strip(), model.lower()]
            conn.execute(
                """
                INSERT OR REPLACE INTO canonical_products
                (canonical_id, brand, model, ref, color, gender, category, variant, image_url, aliases)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    canonical_id,
                    brand,
                    model,
                    ref,
                    "",
                    _guess_gender(model),
                    "sneakers",
                    _guess_variant(model),
                    str(meta.get("source_url") or ""),
                    json.dumps(sorted({a for a in aliases if a}), ensure_ascii=True),
                ),
            )
            count += 1
    return count


if __name__ == "__main__":
    seeded = seed_catalog()
    print(json.dumps({"seeded": seeded}, indent=2))
