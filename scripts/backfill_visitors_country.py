#!/usr/bin/env python3
"""Backfill countries for last unknown visitor IPs."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.visitors import backfill_unknown_countries, init_visitors_db


def main() -> None:
    init_visitors_db()
    updated = backfill_unknown_countries(limit=50)
    print(f"updated_rows={updated}")


if __name__ == "__main__":
    main()

