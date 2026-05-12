from datetime import datetime, timezone

from app.pipeline.v2_pipeline import run_v2_pipeline


def test_pipeline_counts():
    rows = [
        {
            "brand": "Adidas",
            "name": "Stan Smith M20324",
            "model": "Stan Smith M20324",
            "price": 89.0,
            "currency": "EUR",
            "url": "http://example.com",
            "source": "shop-a",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        {
            "brand": "",
            "name": "x",
            "model": "x",
            "price": 0,
            "currency": "EUR",
            "url": "http://example.com",
            "source": "shop-b",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    ]
    report = run_v2_pipeline(rows, dry_run=True)
    assert report.raw_count == 2
    assert report.rejected_filter >= 1
