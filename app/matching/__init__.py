"""Strict matching layer for v2 pipeline."""

from app.matching.canonical import CanonicalProduct, load_catalog
from app.matching.matcher import MatchResult, match_product

__all__ = ["CanonicalProduct", "MatchResult", "load_catalog", "match_product"]
