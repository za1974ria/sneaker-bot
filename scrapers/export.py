"""CSV export for aggregated market rows."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

CSV_COLUMNS = [
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


def export_market_live(rows: list[dict[str, Any]], output_path: Path) -> None:
    """
    Write market_live.csv in a format compatible with existing app routes.
    """
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    with tmp_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in CSV_COLUMNS})
    tmp_path.replace(output_path)

