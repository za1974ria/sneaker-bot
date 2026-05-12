#!/usr/bin/env python3
"""
Fetch sneaker product images via Bing Image Search (no API key needed).

Searches Bing Images for each model, extracts direct image URLs from
the `murl` fields, downloads and validates each image (min 280px sides,
reasonable aspect ratio). Saves model-specific product photos.

All images from this source are saved as verified=False (not from official
brand sites), but will be high-quality, model-specific product shots.

Usage:
  ./venv/bin/python scripts/fetch_bing_images.py
  ./venv/bin/python scripts/fetch_bing_images.py --model "Samba OG"
  ./venv/bin/python scripts/fetch_bing_images.py --force
  ./venv/bin/python scripts/fetch_bing_images.py --dry-run
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "static" / "sneakers_db.json"
STATIC_IMAGES = ROOT / "static" / "images"
NO_IMAGE = "/static/no-image.png"

OFFICIAL_DOMAINS = (
    "secure-images.nike.com",
    "nike.com",
    "adidas.com",
    "assets.adidas.com",
    "brand.assets.adidas.com",
    "reebok.com",
    "puma.com",
    "images.puma.com",
    "newbalance.com",
    "newbalance.eu",
    "nb.com",
    "asics.com",
    "salomon.com",
    "vans.com",
    "converse.com",
    "on-running.com",
    "on.com",
)
STOCKX_DOMAINS = ("images.stockx.com", "stockx.com")

UA_BING = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
UA_IMG = {
    "User-Agent": UA_BING,
    "Accept": "image/webp,image/*,*/*;q=0.8",
}

MIN_SIDE = 280
MAX_BYTES = 10 * 1024 * 1024
BING_SLEEP = 2.0   # between Bing requests (be polite)
IMG_SLEEP = 0.5    # between image downloads
MAX_IMAGES_PER_MODEL = 15


def model_slug(model: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (model or "").lower()).strip("-")
    return (s[:120] or "model").strip("-")


def host_kind(url: str) -> str:
    """Match by exact hostname or subdomain only (prevents substring false positives)."""
    h = (urlparse(url).hostname or "").lower()
    for d in OFFICIAL_DOMAINS:
        if h == d or h.endswith("." + d):
            return "official"
    for d in STOCKX_DOMAINS:
        if h == d or h.endswith("." + d):
            return "stockx"
    return "other"


def bad_url(url: str) -> bool:
    u = url.lower()
    return any(x in u for x in (
        "logo", "favicon", "sprite", "/icon", "badge", "1x1",
        "collage", "banner", "avatar", "profile", "thumbnail",
    ))


def brand_in_url(brand: str, model: str, url: str) -> bool:
    """Check if brand or key model words appear in the URL path."""
    path = urlparse(url).path.lower()
    brand_slug = re.sub(r"[^a-z0-9]", "-", brand.lower()).strip("-")
    model_words = [w for w in re.sub(r"[^a-z0-9]", " ", model.lower()).split() if len(w) > 2]
    # At least brand OR 2+ model words in URL path
    brand_ok = brand_slug[:5] in path
    model_hits = sum(1 for w in model_words if w in path)
    return brand_ok or model_hits >= 2


def bing_image_urls(brand: str, model: str, ref: str = "") -> list[tuple[str, str]]:
    """
    Returns list of (url, host_kind) tuples found on Bing Images.
    Tries multiple query variants.
    """
    queries = []
    if ref:
        queries.append(f"{brand} {model} {ref}")
    queries.append(f"{brand} {model} sneaker product")
    queries.append(f"{brand} {model} chaussure")

    seen: set[str] = set()
    results: list[tuple[str, str]] = []

    for q in queries:
        if len(results) >= MAX_IMAGES_PER_MODEL:
            break
        try:
            resp = requests.get(
                "https://www.bing.com/images/search",
                params={"q": q, "count": "30", "mkt": "fr-FR", "first": "1"},
                headers={
                    "User-Agent": UA_BING,
                    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
                    "Accept": "text/html,application/xhtml+xml",
                    "Referer": "https://www.bing.com/",
                },
                timeout=15,
            )
            if resp.status_code != 200:
                continue
            html = resp.text
            # Extract murl fields (direct image URLs)
            murls = re.findall(r'murl&quot;:&quot;(https?://[^&"]+)&quot;', html)
            for raw_url in murls:
                url = unquote(raw_url).strip()
                if not url or url in seen or bad_url(url):
                    continue
                seen.add(url)
                kind = host_kind(url)
                results.append((url, kind))
        except Exception as e:
            print(f"  [bing] query={q!r}: {e}", file=sys.stderr)
        time.sleep(BING_SLEEP)

    # Sort: official/stockx first, then others
    order = {"official": 0, "stockx": 1, "other": 2}
    results.sort(key=lambda x: order.get(x[1], 9))
    return results


def download(url: str) -> bytes | None:
    try:
        resp = requests.get(url, headers=UA_IMG, timeout=20, stream=True)
        if resp.status_code != 200:
            return None
        buf = io.BytesIO()
        n = 0
        for ch in resp.iter_content(65536):
            n += len(ch)
            if n > MAX_BYTES:
                return None
            buf.write(ch)
        return buf.getvalue()
    except Exception:
        return None


def validate_and_save(data: bytes, out: Path) -> bool:
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        im.load()
        w, h = im.size
        if min(w, h) < MIN_SIDE:
            return False
        if max(w, h) / max(min(w, h), 1) > 4.0:
            return False
        if im.mode == "RGBA":
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[3])
            im = bg
        elif im.mode != "RGB":
            im = im.convert("RGB")
        out.parent.mkdir(parents=True, exist_ok=True)
        im.save(out, "JPEG", quality=90, optimize=True)
        return True
    except Exception:
        return False


def process(db: dict, model: str, dry: bool, force: bool) -> str:
    entry = db.get(model)
    if not isinstance(entry, dict):
        return "skip: no entry"
    brand = str(entry.get("brand") or "").strip()
    ref = str(entry.get("ref") or "").strip()
    if not brand:
        return "skip: no brand"

    slug = model_slug(model)
    out = STATIC_IMAGES / f"{slug}.jpg"
    rel = f"/static/images/{slug}.jpg"

    cur_img = str(entry.get("image") or "")
    already_verified_local = (
        out.is_file()
        and out.stat().st_size > 0
        and cur_img == rel
        and "no-image" not in cur_img
        and entry.get("verified") is True
    )
    if already_verified_local and not force:
        return "⏭  already verified local"

    if dry:
        return f"dry-run: {brand} {model} [{ref}]"

    candidates = bing_image_urls(brand, model, ref)
    if not candidates:
        if not already_local:
            entry["image"] = NO_IMAGE
        return "❌ no Bing results"

    for url, kind in candidates:
        raw = download(url)
        if not raw:
            time.sleep(IMG_SLEEP)
            continue
        if validate_and_save(raw, out):
            entry["image"] = rel
            entry["source_url"] = url
            entry["source_title"] = f"{brand} {model} — bing/{kind}"
            entry["verified"] = kind in ("official", "stockx")
            tag = "verified" if entry["verified"] else "bing"
            return f"✔ [{tag}/{kind}] {rel}"
        time.sleep(IMG_SLEEP)

    if not already_local:
        entry["image"] = NO_IMAGE
    return "❌ all downloads failed"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", type=str, default="")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db: dict = json.loads(DB_PATH.read_text(encoding="utf-8"))
    STATIC_IMAGES.mkdir(parents=True, exist_ok=True)

    if args.model:
        models = [args.model] if args.model in db else []
        if not models:
            print(f"Unknown model: {args.model}", file=sys.stderr)
            return 1
    else:
        # Only process unverified models (missing image or verified=False)
        models = sorted(
            (k for k, v in db.items()
             if isinstance(v, dict) and (
                 args.force
                 or not v.get("verified")
             )),
            key=str.lower,
        )

    print(f"Target: {len(models)} models to process\n")
    ok = fail = skip = 0

    for m in models:
        result = process(db, m, args.dry_run, args.force)
        print(f"{m} → {result}", flush=True)
        if result.startswith("✔"):
            ok += 1
        elif result.startswith("⏭"):
            skip += 1
        elif not result.startswith("dry"):
            fail += 1
        if not args.dry_run:
            time.sleep(0.2)  # small gap between models

    if not args.dry_run:
        DB_PATH.write_text(json.dumps(db, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    verified = sum(1 for v in db.values() if isinstance(v, dict) and v.get("verified"))
    total = len(db)
    print(f"\nSummary: ✔={ok}, ⏭={skip}, ❌={fail}")
    print(f"✅ verified (official/StockX) in DB: {verified}/{total}")
    print(f"📸 with local image: {sum(1 for v in db.values() if isinstance(v, dict) and v.get('image') and 'no-image' not in str(v.get('image','')) and str(v.get('image','')).startswith('/static/images/'))}/{total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
