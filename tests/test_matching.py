from datetime import datetime, timezone

from app.matching.canonical import CanonicalProduct
from app.matching.matcher import match_product
from app.cleaning.normalizer import CleanRow


def _base_row(**kwargs) -> CleanRow:
    payload = {
        "name": "adidas stan smith white green",
        "brand": "Adidas",
        "model_guess": "Stan Smith",
        "ref": None,
        "color": "White/Green",
        "size": "42",
        "price_eur": 89.9,
        "url": "https://shop.example/stan-smith",
        "source": "test-shop",
        "timestamp": datetime.now(timezone.utc),
    }
    payload.update(kwargs)
    return CleanRow(**payload)


def _base_canonical(**kwargs) -> CanonicalProduct:
    payload = {
        "canonical_id": "adidas-stan-smith-m20324",
        "brand": "Adidas",
        "model": "Stan Smith",
        "ref": "M20324",
        "color": "White/Green",
        "gender": "M",
        "category": "sneakers",
        "variant": None,
        "image_url": "https://img.example/stan-smith.jpg",
        "aliases": ["stan smith m20324", "adidas stan smith"],
    }
    payload.update(kwargs)
    return CanonicalProduct(**payload)


def test_stan_smith_kids_not_matched_to_adult():
    kids_row = _base_row(ref="FX7520", model_guess="Stan Smith J")
    adult = _base_canonical(ref="M20324", gender="M")
    result = match_product(kids_row, [adult])
    assert result.confidence == "NO_MATCH"


def test_stan_smith_platform_not_matched_to_classic():
    platform_row = _base_row(name="adidas stan smith platform white")
    classic = _base_canonical(variant=None)
    result = match_product(platform_row, [classic])
    assert result.confidence == "NO_MATCH"


def test_exact_sku_match_is_strong():
    row = _base_row(ref="M20324")
    canonical = _base_canonical(ref="M20324")
    result = match_product(row, [canonical])
    assert result.confidence == "strong"
    assert result.canonical_id == canonical.canonical_id
