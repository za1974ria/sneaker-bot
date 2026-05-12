from datetime import datetime, timedelta, timezone

from app.cleaning.filters import is_row_valid
from app.cleaning.normalizer import normalize_row


def test_filter_rejects_old_row():
    row = {
        "brand": "Adidas",
        "name": "Stan Smith",
        "price": 80,
        "currency": "EUR",
        "url": "http://example.com",
        "timestamp": (datetime.now(timezone.utc) - timedelta(days=31)).isoformat(),
        "skip_link_check": True,
    }
    ok, reason = is_row_valid(row)
    assert not ok
    assert reason == "row_too_old"


def test_normalize_row_converts_and_extracts():
    row = {
        "brand": "adidas originals",
        "name": "Stan Smith NEW M20324 WHITE",
        "price": "100",
        "currency": "USD",
        "url": "http://example.com",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    normalized = normalize_row(row)
    assert normalized.brand == "Adidas"
    assert normalized.ref == "M20324"
    assert normalized.price_eur > 0
