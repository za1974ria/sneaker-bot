"""Orchestrator for strict v2 cleaning + matching pipeline."""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.cleaning import filters, normalizer
from app.cleaning.dedup import dedupe
from app.matching import anti_aberration, canonical, matcher

DB_PATH = Path(__file__).resolve().parents[2] / "data" / "sneakerbot.db"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PipelineReport:
    raw_count: int
    rejected_filter: int
    rejected_match: int
    rejected_outlier: int
    final_count: int
    checksum_ok: bool
    filter_breakdown: dict[str, int]
    outlier_breakdown: dict[str, int]
    outlier_config: dict[str, Any]
    top_rejections: list[dict[str, Any]]


def persist_to_matched_rows_v2(rows: list[matcher.MatchResult], db_path: Path | None = None) -> None:
    target = db_path or DB_PATH
    with sqlite3.connect(str(target), timeout=20) as conn:
        conn.executemany(
            """
            INSERT INTO matched_rows_v2(raw_row_id, canonical_id, confidence, score, price_eur, source, url)
            VALUES(NULL, ?, ?, ?, ?, ?, ?)
            """,
            [(r.canonical_id, r.confidence, r.score, r.price_eur, r.source, r.url) for r in rows if r.canonical_id],
        )


def run_v2_pipeline(raw_rows: list[dict], dry_run: bool = True) -> PipelineReport:
    raw_count = len(raw_rows)
    valid: list[dict[str, Any]] = []
    filter_reasons: Counter[str] = Counter()
    match_reasons: Counter[str] = Counter()
    outlier_reasons: Counter[str] = Counter()
    for row in raw_rows:
        ok, reason = filters.is_row_valid(row)
        if ok:
            valid.append(row)
        else:
            filter_reason = reason or "unknown_filter"
            filter_reasons[filter_reason] += 1
            logger.debug("[FILTER] %s → %s", filter_reason, str(row.get("url", "?"))[:60])

    clean = [normalizer.normalize_row(r) for r in valid]
    clean = dedupe(clean)
    catalog = canonical.load_catalog()
    matched = [matcher.match_product(c, catalog) for c in clean]
    kept = [m for m in matched if m.confidence != "NO_MATCH"]
    for dropped in (m for m in matched if m.confidence == "NO_MATCH"):
        match_reasons[dropped.reason] += 1

    outlier_result = anti_aberration.filter_outliers(kept)
    final = outlier_result.kept_rows
    outlier_reasons.update(outlier_result.breakdown)

    rejected_filter = raw_count - len(valid)
    logger.info(
        "[FILTER SUMMARY] %d/%d lignes rejetées : %s",
        rejected_filter,
        raw_count,
        dict(filter_reasons),
    )
    if rejected_filter == 0:
        logger.info("[FILTER] 0 rejet sur données réelles — scrapers pré-nettoient déjà, OK")
    rejected_match = len(clean) - len(kept)
    rejected_outlier = len(outlier_result.dropped_rows)
    final_count = len(final)
    checksum_ok = (rejected_filter + rejected_match + rejected_outlier + final_count) == raw_count
    if not checksum_ok:
        logger.warning(
            "V2 checksum mismatch raw=%d filter=%d match=%d outlier=%d final=%d",
            raw_count,
            rejected_filter,
            rejected_match,
            rejected_outlier,
            final_count,
        )

    if not dry_run:
        persist_to_matched_rows_v2(final)

    rejection_reasons = Counter()
    rejection_reasons.update(filter_reasons)
    rejection_reasons.update(match_reasons)
    rejection_reasons.update(outlier_reasons)
    top_rejections = [{"reason": reason, "count": count} for reason, count in rejection_reasons.most_common(10)]
    report = PipelineReport(
        raw_count=raw_count,
        rejected_filter=rejected_filter,
        rejected_match=rejected_match,
        rejected_outlier=rejected_outlier,
        final_count=final_count,
        checksum_ok=checksum_ok,
        filter_breakdown=dict(filter_reasons),
        outlier_breakdown=dict(outlier_reasons),
        outlier_config=dict(anti_aberration.OUTLIER_CONFIG),
        top_rejections=top_rejections,
    )
    return report


def report_to_dict(report: PipelineReport) -> dict[str, Any]:
    return asdict(report)
