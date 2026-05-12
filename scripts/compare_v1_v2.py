"""Compare v1 vs v2 pipeline results on 10 pilot models.

V1 source : data/market_fr_sources.csv  (raw aggregated per shop, no outlier filter)
V2 source : v2 pipeline run inline       (strict match + anti-aberration)

Output    : /tmp/v1_v2_comparison.json + console summary
"""

from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.cleaning import filters, normalizer
from app.cleaning.dedup import dedupe
from app.matching import anti_aberration, canonical, matcher

# ─── Pilot model mapping ──────────────────────────────────────────────────────
# (display_name, canonical_id, csv_brand, csv_model)
# canonical_id = None  → not in catalog (Ultraboost 22)
# csv_model    = None  → not in CSV (marked as unavailable)
PILOT_MODELS: list[tuple[str, str | None, str, str | None]] = [
    ("Nike Air Force 1 Low",        "nike-air-force-1-low-cw2288-111",        "Nike",        "Air Force 1 Low"),
    ("Nike Air Max 90",             "nike-air-max-90-essential-aj1285-111",   "Nike",        "Air Max 90 Essential"),
    ("Adidas Stan Smith",           "adidas-stan-smith-m20324",               "Adidas",      "Stan Smith"),
    ("Adidas Ultraboost 22",        None,                                      "Adidas",      None),           # absent du catalogue
    ("New Balance 574",             "new-balance-574-core-ml574evw",          "New Balance", "574 Core"),
    ("New Balance 990v5",           "new-balance-990v6-made-in-usa-m990gl6",  "New Balance", "990v6 Made in USA"),  # 990v5 absent → 990v6
    ("Jordan 1 Retro High OG",      "nike-air-jordan-1-high-og-555088-101",   "Nike",        "Air Jordan 1 High OG"),
    ("Converse Chuck Taylor All Star","converse-all-star-low-m9166c",         "Converse",    "All Star Low"),
    ("Vans Old Skool",              "vans-old-skool-overt-vn000d3hy28",       "Vans",        "Old Skool Overt"),
    ("Asics Gel-Kayano 14",         "asics-gel-kayano-14-1201a019-100",       "Asics",       "Gel-Kayano 14"),
]

CSV_PATH = ROOT / "data" / "market_fr_sources.csv"


# ─── V1 : load from CSV ───────────────────────────────────────────────────────

def _load_v1() -> dict[str, dict[str, Any]]:
    """Return {'{brand}|{model}': {price_min, price_max, price_avg, price_count, boutiques}}."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            updated_raw = row.get("updated_at", "").strip()
            try:
                dt = datetime.fromisoformat(updated_raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt < cutoff:
                    continue
            except ValueError:
                continue
            price_raw = row.get("price_avg", "").strip()
            if not price_raw:
                continue
            key = f"{row['brand'].strip()}|{row['model'].strip()}"
            groups[key].append({
                "price": float(price_raw),
                "shop": row.get("shop", "").strip(),
            })

    result: dict[str, dict[str, Any]] = {}
    for key, entries in groups.items():
        prices = [e["price"] for e in entries]
        result[key] = {
            "price_min": round(min(prices), 2),
            "price_max": round(max(prices), 2),
            "price_avg": round(mean(prices), 2),
            "price_count": len(prices),
            "boutiques": sorted({e["shop"] for e in entries if e["shop"]}),
        }
    return result


# ─── V2 : run pipeline inline ─────────────────────────────────────────────────

def _build_pipeline_rows() -> list[dict[str, Any]]:
    """Load & standardise CSV rows for the pipeline (mirrors run_v2_dry_run logic)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    rows: list[dict[str, Any]] = []
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        for record in csv.DictReader(f):
            updated_at = record.get("updated_at", "").strip()
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
            brand  = record.get("brand", "").strip()
            model  = record.get("model", "").strip()
            shop   = record.get("shop",  "").strip()
            slug   = f"{brand.lower().replace(' ', '-')}/{model.lower().replace(' ', '-')}"
            rows.append({
                "brand":          brand,
                "name":           model,
                "model":          model,
                "price":          float(price_raw),
                "currency":       "EUR",
                "url":            f"https://sneakerbot.shop/p/{slug}",
                "source":         shop,
                "timestamp":      updated_at,
                "skip_link_check": True,
            })
    return rows


def _run_v2() -> dict[str, dict[str, Any]]:
    """Return {canonical_id: {price_min, price_max, price_avg, price_count, boutiques, outliers_removed, confidence}}."""
    raw_rows  = _build_pipeline_rows()
    valid     = [r for r in raw_rows if filters.is_row_valid(r)[0]]
    clean     = [normalizer.normalize_row(r) for r in valid]
    clean     = dedupe(clean)
    cat       = canonical.load_catalog()
    matched   = [matcher.match_product(c, cat) for c in clean]
    kept_pre  = [m for m in matched if m.confidence != "NO_MATCH"]

    # Track per-canonical counts before outlier filter
    pre_counts: dict[str, int] = defaultdict(int)
    for m in kept_pre:
        if m.canonical_id:
            pre_counts[m.canonical_id] += 1

    outlier_result = anti_aberration.filter_outliers(kept_pre)
    final          = outlier_result.kept_rows

    # Group final results by canonical_id
    groups: dict[str, list[matcher.MatchResult]] = defaultdict(list)
    for m in final:
        if m.canonical_id:
            groups[m.canonical_id].append(m)

    result: dict[str, dict[str, Any]] = {}
    for cid, items in groups.items():
        prices    = [m.price_eur for m in items]
        pre_cnt   = pre_counts.get(cid, len(items))
        outliers  = pre_cnt - len(items)
        # confidence = best label seen
        labels    = {m.confidence for m in items}
        conf      = "high" if "strong" in labels else ("medium" if "medium" in labels else "low")
        result[cid] = {
            "price_min":       round(min(prices), 2),
            "price_max":       round(max(prices), 2),
            "price_avg":       round(mean(prices), 2),
            "price_count":     len(items),
            "boutiques":       sorted({m.source for m in items if m.source}),
            "outliers_removed": max(outliers, 0),
            "confidence":      conf,
        }
    return result


# ─── Verdict ──────────────────────────────────────────────────────────────────

def _verdict(v1_cnt: int, v2_cnt: int, delta_count_pct: float, outliers_removed: int) -> str:
    # Both sides have no data → product absent from source, not a pipeline issue
    if v1_cnt == 0 and v2_cnt == 0:
        return "not_available"
    if delta_count_pct == 0.0:
        return "equivalent"
    if delta_count_pct < -30.0:
        return "v2_too_restrictive"
    if -30.0 <= delta_count_pct <= 0.0 and outliers_removed > 0:
        return "v2_more_precise"
    return "equivalent"


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    print("⏳ Loading v1 data from market_fr_sources.csv …")
    v1_data = _load_v1()

    print("⏳ Running v2 pipeline …")
    v2_data = _run_v2()

    results: list[dict[str, Any]] = []
    for display_name, canonical_id, csv_brand, csv_model in PILOT_MODELS:
        v1_key   = f"{csv_brand}|{csv_model}" if csv_model else None
        v1_entry = v1_data.get(v1_key, {}) if v1_key else {}
        v2_entry = v2_data.get(canonical_id, {}) if canonical_id else {}

        v1_block = {
            "price_count": v1_entry.get("price_count", 0),
            "price_min":   v1_entry.get("price_min"),
            "price_max":   v1_entry.get("price_max"),
            "price_avg":   v1_entry.get("price_avg"),
            "boutiques":   v1_entry.get("boutiques", []),
        }
        v2_block = {
            "price_count":     v2_entry.get("price_count", 0),
            "price_min":       v2_entry.get("price_min"),
            "price_max":       v2_entry.get("price_max"),
            "price_avg":       v2_entry.get("price_avg"),
            "boutiques":       v2_entry.get("boutiques", []),
            "outliers_removed": v2_entry.get("outliers_removed", 0),
            "confidence":      v2_entry.get("confidence", "low"),
        }

        v1_avg  = v1_block["price_avg"] or 0.0
        v2_avg  = v2_block["price_avg"] or 0.0
        v1_cnt  = v1_block["price_count"]
        v2_cnt  = v2_block["price_count"]

        delta_avg  = round(v2_avg - v1_avg, 2) if (v1_avg and v2_avg) else None
        delta_cnt  = v2_cnt - v1_cnt
        if v1_cnt == 0 and v2_cnt == 0:
            pct_change = 0.0
        elif v1_cnt == 0:
            pct_change = 100.0
        else:
            pct_change = (v2_cnt - v1_cnt) / v1_cnt * 100

        record = {
            "model":          display_name,
            "canonical_id":   canonical_id,
            "v1":             v1_block,
            "v2":             v2_block,
            "delta_avg_eur":  delta_avg,
            "delta_count":    delta_cnt,
            "delta_count_pct": round(pct_change, 1),
            "verdict":        _verdict(v1_cnt, v2_cnt, pct_change, v2_block["outliers_removed"]),
        }
        results.append(record)

    # ── Summary ──
    verdicts    = [r["verdict"] for r in results]
    n_precise   = verdicts.count("v2_more_precise")
    n_equiv     = verdicts.count("equivalent")
    n_restrict  = verdicts.count("v2_too_restrictive")
    n_na        = verdicts.count("not_available")

    output_path = Path("/tmp/v1_v2_comparison.json")
    output_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\n{'─'*60}")
    print(f"📊 V1 vs V2 — Résumé sur {len(results)} modèles pilotes")
    print(f"{'─'*60}")
    for r in results:
        v1c = r["v1"]["price_count"]
        v2c = r["v2"]["price_count"]
        v1a = r["v1"]["price_avg"]
        v2a = r["v2"]["price_avg"]
        out = r["v2"]["outliers_removed"]
        vtag = {"v2_more_precise": "✅", "equivalent": "➖", "v2_too_restrictive": "⚠️", "not_available": "—"}[r["verdict"]]
        print(
            f"  {vtag} {r['model']:<35} "
            f"v1={v1c} boutiques(avg={v1a})  "
            f"v2={v2c} boutiques(avg={v2a})  "
            f"outliers={out}  Δ={r['delta_count_pct']}%  → {r['verdict']}"
        )

    print(f"\n{'─'*60}")
    print(f"  {n_precise}/10  : v2_more_precise")
    print(f"  {n_equiv}/10    : equivalent")
    print(f"  {n_restrict}/10  : v2_too_restrictive")
    print(f"  {n_na}/10    : not_available (absent du CSV source)")
    print(f"{'─'*60}")
    print(f"✅ Rapport exporté : {output_path}")


if __name__ == "__main__":
    main()
