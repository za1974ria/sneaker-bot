"""Canonical catalog model and loader."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[2] / "data" / "sneakerbot.db"


@dataclass(frozen=True)
class CanonicalProduct:
    canonical_id: str
    brand: str
    model: str
    ref: str
    color: str
    gender: str
    category: str
    variant: str | None
    image_url: str
    aliases: list[str]


def load_catalog(db_path: Path | None = None) -> list[CanonicalProduct]:
    target = db_path or DB_PATH
    if not target.exists():
        return []
    with sqlite3.connect(str(target), timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT canonical_id, brand, model, ref, color, gender, category, variant, image_url, aliases
                FROM canonical_products
                """
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    products: list[CanonicalProduct] = []
    for row in rows:
        try:
            aliases = json.loads(str(row["aliases"] or "[]"))
        except json.JSONDecodeError:
            aliases = []
        products.append(
            CanonicalProduct(
                canonical_id=str(row["canonical_id"]),
                brand=str(row["brand"] or ""),
                model=str(row["model"] or ""),
                ref=str(row["ref"] or ""),
                color=str(row["color"] or ""),
                gender=str(row["gender"] or "U"),
                category=str(row["category"] or "sneakers"),
                variant=str(row["variant"]) if row["variant"] is not None else None,
                image_url=str(row["image_url"] or ""),
                aliases=aliases if isinstance(aliases, list) else [],
            )
        )
    return products
