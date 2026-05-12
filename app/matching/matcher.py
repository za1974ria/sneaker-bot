"""Strict product matcher for v2 pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

from app.cleaning.normalizer import CleanRow
from app.matching.canonical import CanonicalProduct
from app.matching.confidence import label_from_score

try:
    from rapidfuzz import fuzz
except ImportError:  # pragma: no cover
    fuzz = None


@dataclass(frozen=True)
class MatchResult:
    canonical_id: str | None
    confidence: str
    score: float
    reason: str
    price_eur: float
    source: str
    url: str


def _similarity(left: str, right: str) -> float:
    if fuzz is not None:
        return float(fuzz.token_set_ratio(left, right))
    return SequenceMatcher(None, left, right).ratio() * 100.0


def _has_kids_hint(text: str) -> bool:
    lowered = text.lower()
    hints = (" kids ", " kid ", " junior ", " jr ", " enfant ", " youth ", " j ")
    return any(h in f" {lowered} " for h in hints)


def match_product(clean_row: CleanRow, catalog: list[CanonicalProduct]) -> MatchResult:
    row_ref = (clean_row.ref or "").upper().strip()
    row_name = clean_row.name.strip().lower()
    row_brand = clean_row.brand.strip().lower()

    if "platform" in row_name:
        for item in catalog:
            if item.brand.strip().lower() == row_brand and item.variant != "platform":
                return MatchResult(None, "NO_MATCH", 0.0, "platform_variant_mismatch", clean_row.price_eur, clean_row.source, clean_row.url)

    if row_ref:
        for item in catalog:
            if row_ref == (item.ref or "").upper().strip():
                return MatchResult(item.canonical_id, "strong", 100.0, "exact_ref_match", clean_row.price_eur, clean_row.source, clean_row.url)

    best_item: CanonicalProduct | None = None
    best_score = 0.0
    for item in catalog:
        if item.brand.strip().lower() != row_brand:
            continue
        aliases = item.aliases or [f"{item.brand} {item.model}", item.ref]
        candidate_score = max(_similarity(row_name, alias.lower()) for alias in aliases if alias)
        if candidate_score > best_score:
            best_score = candidate_score
            best_item = item

    if not best_item:
        return MatchResult(None, "NO_MATCH", 0.0, "brand_not_found", clean_row.price_eur, clean_row.source, clean_row.url)

    # Specificity guard: if the row name has extra tokens not in the best-matching
    # canonical model, prefer a more specific catalog entry when one exists.
    # Example: "stan smith recon" → prefer "Stan Smith Recon" over "Stan Smith".
    row_tokens  = set(row_name.split())
    best_tokens = set(best_item.model.lower().split())
    extra_tokens = row_tokens - best_tokens
    if extra_tokens:
        for item in catalog:
            if item is best_item or item.brand.strip().lower() != row_brand:
                continue
            item_tokens = set(item.model.lower().split())
            if extra_tokens.issubset(item_tokens) and best_tokens.issubset(item_tokens):
                alt_aliases = item.aliases or [f"{item.brand} {item.model}".lower(), item.model.lower()]
                alt_score = max(_similarity(row_name, a.lower()) for a in alt_aliases if a)
                if alt_score >= best_score:
                    best_item = item
                    best_score = alt_score
                break

    if best_item.variant == "platform" and "platform" not in row_name:
        return MatchResult(None, "NO_MATCH", best_score, "platform_guard", clean_row.price_eur, clean_row.source, clean_row.url)

    if best_item.gender == "K" and clean_row.price_eur > 90:
        return MatchResult(None, "NO_MATCH", best_score, "kids_price_guard", clean_row.price_eur, clean_row.source, clean_row.url)

    if best_item.gender != "K" and (_has_kids_hint(clean_row.model_guess) or _has_kids_hint(clean_row.name)):
        return MatchResult(None, "NO_MATCH", best_score, "kids_to_adult_guard", clean_row.price_eur, clean_row.source, clean_row.url)

    confidence = label_from_score(best_score)
    if confidence == "NO_MATCH":
        return MatchResult(None, "NO_MATCH", best_score, "low_similarity", clean_row.price_eur, clean_row.source, clean_row.url)
    return MatchResult(best_item.canonical_id, confidence, best_score, "alias_similarity", clean_row.price_eur, clean_row.source, clean_row.url)
