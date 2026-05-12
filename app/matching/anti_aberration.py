"""Outlier filtering based on canonical medians."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from statistics import median

from app.matching.matcher import MatchResult

OUTLIER_CONFIG = {
    "method": "sigma",
    "threshold": 2.5,
    "min_prices_required": 3,
    "lower_multiplier": 0.3,
    "upper_multiplier": 2.5,
}


@dataclass(frozen=True)
class OutlierFilterResult:
    kept_rows: list[MatchResult]
    dropped_rows: list[MatchResult]
    breakdown: dict[str, int]


def filter_outliers(matched_rows: list[MatchResult]) -> OutlierFilterResult:
    by_canonical: dict[str, list[MatchResult]] = defaultdict(list)
    for row in matched_rows:
        if row.canonical_id:
            by_canonical[row.canonical_id].append(row)

    final: list[MatchResult] = []
    dropped: list[MatchResult] = []
    breakdown: dict[str, int] = {"high_dispersion": 0, "low_confidence_market": 0}
    for _, rows in by_canonical.items():
        prices = [r.price_eur for r in rows]
        med = median(prices)
        lower = med * float(OUTLIER_CONFIG["lower_multiplier"])
        upper = med * float(OUTLIER_CONFIG["upper_multiplier"])
        in_range = [r for r in rows if lower <= r.price_eur <= upper]
        outliers = [r for r in rows if r not in in_range]
        if outliers:
            breakdown["high_dispersion"] += len(outliers)
            dropped.extend(outliers)

        if len(in_range) < int(OUTLIER_CONFIG["min_prices_required"]):
            tagged = [
                MatchResult(
                    canonical_id=r.canonical_id,
                    confidence=r.confidence,
                    score=r.score,
                    reason=f"{r.reason}|low_confidence_market",
                    price_eur=r.price_eur,
                    source=r.source,
                    url=r.url,
                )
                for r in in_range
            ]
            breakdown["low_confidence_market"] += len(tagged)
            dropped.extend(tagged)
            continue

        final.extend(in_range)
    return OutlierFilterResult(kept_rows=final, dropped_rows=dropped, breakdown=breakdown)
