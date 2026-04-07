"""Outlier filtering for price series."""

from __future__ import annotations


def filter_outliers(values: list[float]) -> list[float]:
    if not values:
        return []
    sorted_vals = sorted(values)
    if len(sorted_vals) < 5:
        return sorted_vals
    trim = max(1, int(len(sorted_vals) * 0.1))
    core = sorted_vals[trim:-trim] if len(sorted_vals) - (2 * trim) >= 2 else sorted_vals
    return core

