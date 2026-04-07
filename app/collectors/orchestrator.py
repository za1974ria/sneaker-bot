"""Pipeline orchestrator: collect -> normalize -> aggregate -> export."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.aggregate.stats import aggregate_market
from app.collectors.tiers import TIERS
from app.export.writer import write_market_live
from app.monitoring.logger import configure_logger
from app.monitoring.metrics import PipelineMetrics
from app.normalize.dedupe import dedupe_rows
from app.normalize.mapper import normalize_rows


def _collect_tier(
    tier_name: str,
    tier_scrapers: dict[str, Any],
    logger,
    metrics: PipelineMetrics,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for source_name, scraper_fn in tier_scrapers.items():
        try:
            batch = scraper_fn()
            if not isinstance(batch, list):
                logger.warning("tier=%s source=%s invalid_payload", tier_name, source_name)
                metrics.mark_source_failed(source_name)
                continue
            rows.extend(batch)
            metrics.mark_source_ok(source_name, len(batch))
            logger.info("tier=%s source=%s rows=%d", tier_name, source_name, len(batch))
        except Exception as e:  # noqa: BLE001
            logger.exception("tier=%s source=%s error=%s", tier_name, source_name, e)
            metrics.mark_source_failed(source_name)
            continue
    return rows


def run_collection_pipeline(output_csv: str | Path = "market_live.csv") -> dict[str, Any]:
    logger = configure_logger("sneaker.collectors")
    metrics = PipelineMetrics()

    raw_rows: list[dict[str, Any]] = []
    for tier_name in ("core", "extension", "long_tail"):
        raw_rows.extend(_collect_tier(tier_name, TIERS.get(tier_name, {}), logger, metrics))

    normalized = normalize_rows(raw_rows, default_source="unknown")
    normalized = dedupe_rows(normalized)
    metrics.rows_normalized = len(normalized)

    aggregated = aggregate_market(normalized)
    metrics.rows_aggregated = len(aggregated)

    out_path = Path(output_csv)
    if aggregated:
        write_market_live(aggregated, out_path)
        logger.info("pipeline_export rows=%d path=%s", len(aggregated), out_path)
        status = "ok"
    else:
        logger.warning("pipeline_export no data")
        status = "no_data"

    return {
        "status": status,
        "output_csv": str(out_path),
        "sources_total": metrics.sources_total,
        "sources_ok": metrics.sources_ok,
        "sources_failed": metrics.sources_failed,
        "rows_raw": metrics.rows_raw,
        "rows_normalized": metrics.rows_normalized,
        "rows_aggregated": metrics.rows_aggregated,
    }

