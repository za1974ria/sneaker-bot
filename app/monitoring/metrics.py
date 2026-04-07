"""Lightweight in-memory metrics for pipeline reliability."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PipelineMetrics:
    sources_total: int = 0
    sources_ok: int = 0
    sources_failed: int = 0
    rows_raw: int = 0
    rows_normalized: int = 0
    rows_aggregated: int = 0
    by_source: dict[str, int] = field(default_factory=dict)

    def mark_source_ok(self, source: str, rows: int) -> None:
        self.sources_total += 1
        self.sources_ok += 1
        self.rows_raw += rows
        self.by_source[source] = self.by_source.get(source, 0) + rows

    def mark_source_failed(self, source: str) -> None:
        self.sources_total += 1
        self.sources_failed += 1
        self.by_source[source] = self.by_source.get(source, 0)

