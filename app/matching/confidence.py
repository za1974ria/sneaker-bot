"""Confidence helpers for strict matcher."""

from __future__ import annotations


def label_from_score(score: float) -> str:
    if score >= 88:
        return "strong"
    if score >= 75:
        return "medium"
    return "NO_MATCH"
