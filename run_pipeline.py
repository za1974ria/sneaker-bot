#!/usr/bin/env python3
"""Main production pipeline entrypoint."""

from __future__ import annotations

from pathlib import Path

from app.collectors.orchestrator import run_collection_pipeline

ROOT = Path(__file__).resolve().parent
OUTPUT_CSV = ROOT / "market_live.csv"

def main() -> int:
    summary = run_collection_pipeline(output_csv=OUTPUT_CSV)
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

