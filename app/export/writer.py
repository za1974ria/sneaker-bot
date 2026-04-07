"""CSV writer for market_live export."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

MARKET_COLUMNS = [
    "product",
    "produit",
    "min",
    "max",
    "avg",
    "variation",
    "trend",
    "timestamp",
    "updated_at",
]


def write_market_live(rows: list[dict[str, Any]], output_path: Path) -> None:
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    with tmp_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=MARKET_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in MARKET_COLUMNS})
    tmp_path.replace(output_path)

