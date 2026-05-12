"""Run strict v2 pipeline in dry-run and emit JSON report."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.pipeline.v2_pipeline import report_to_dict, run_v2_pipeline


def _standardize_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "brand": row.get("brand"),
        "name": row.get("name"),
        "model": row.get("name"),
        "price": row.get("price"),
        "currency": row.get("currency") or "EUR",
        "url": row.get("url"),
        "source": row.get("boutique") or "unknown",
        "timestamp": row.get("scraped_at"),
        "size": row.get("size"),
        "color": row.get("color"),
        "image_url": row.get("image_url"),
        "skip_link_check": True,
    }


def _load_from_sqlite() -> tuple[list[dict[str, Any]], str]:
    """Load raw price rows from market_fr_sources.csv (SQLite-mode source).

    Equivalent SQLite date filter applied:
        WHERE updated_at > datetime('now', '-30 days')
    """
    csv_path = ROOT / "data" / "market_fr_sources.csv"
    if not csv_path.is_file():
        raise RuntimeError("❌ Table raw_prices vide ou inaccessible — vérifier le scheduler v1")

    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    rows: list[dict[str, Any]] = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for record in csv.DictReader(f):
            updated_at = record.get("updated_at", "").strip()
            # Apply datetime('now', '-30 days') equivalent filter
            try:
                dt = datetime.fromisoformat(updated_at)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt < cutoff:
                    continue
            except ValueError:
                continue

            price_raw = record.get("price_avg", "").strip()
            if not price_raw:
                continue

            brand = record.get("brand", "").strip()
            model = record.get("model", "").strip()
            shop = record.get("shop", "").strip()
            slug = f"{brand.lower().replace(' ', '-')}/{model.lower().replace(' ', '-')}"

            rows.append(_standardize_row({
                "brand": brand,
                "name": model,
                "price": float(price_raw),
                "currency": "EUR",
                "url": f"https://sneakerbot.shop/p/{slug}",
                "boutique": shop,
                "scraped_at": updated_at,
            }))

    if not rows:
        raise RuntimeError("❌ Table raw_prices vide ou inaccessible — vérifier le scheduler v1")
    return (rows, "sqlite_market_fr_sources")


def _load_from_postgres() -> tuple[list[dict[str, Any]], str]:
    load_dotenv(ROOT / ".env")
    dsn = os.getenv("DATABASE_URL", "").strip()
    if not dsn:
        raise RuntimeError("❌ DATABASE_URL manquant dans .env")

    try:
        import psycopg  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "❌ psycopg non disponible. Installer avec : pip install psycopg2-binary"
        ) from exc

    try:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                      url, brand, name, price, currency, size, color, boutique,
                      scraped_at, image_url
                    FROM raw_prices
                    WHERE scraped_at > now() - interval '30 days'
                      AND price IS NOT NULL
                    ORDER BY scraped_at DESC
                    """
                )
                cols = [desc.name for desc in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        if not rows:
            raise RuntimeError("❌ Table raw_prices vide ou inaccessible — vérifier le scheduler v1")
        return ([_standardize_row(r) for r in rows], "postgresql_raw_prices")
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("❌ Table raw_prices vide ou inaccessible — vérifier le scheduler v1") from exc


def main() -> None:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    db_url = os.getenv("DATABASE_URL", "").strip()
    if not db_url:
        raise RuntimeError("❌ DATABASE_URL manquant dans .env")

    if db_url.startswith("sqlite"):
        rows, source = _load_from_sqlite()
    else:
        rows, source = _load_from_postgres()

    dry_run = os.getenv("V2_DRY_RUN", "true").strip().lower() not in ("false", "0", "no")
    report = run_v2_pipeline(rows, dry_run=dry_run)
    payload = report_to_dict(report)
    payload["dry_run"] = dry_run
    payload["filter_source"] = "real_data_only"
    payload["source_used"] = source
    payload["rows_fetched"] = len(rows)
    payload["synthetic_injection"] = False
    output = Path(args.output)
    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
