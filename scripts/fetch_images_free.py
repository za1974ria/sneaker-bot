#!/usr/bin/env python3
"""
Fetch sneaker images using free sources (no API key required).

Sources used (in order of preference):
1. Wikimedia Commons / Wikipedia (open-licensed images, directly downloadable)
2. Openverse API (CC-licensed images from Flickr, Wikimedia, etc.)
3. Dynamic Openverse fallback for models not in the hardcoded map

Images from these sources are saved as verified=False (not official brand sources).

Usage:
  ./venv/bin/python scripts/fetch_images_free.py
  ./venv/bin/python scripts/fetch_images_free.py --model "Samba OG"
  ./venv/bin/python scripts/fetch_images_free.py --force
  ./venv/bin/python scripts/fetch_images_free.py --dry-run
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
from urllib.parse import quote_plus

import requests

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "static" / "sneakers_db.json"
STATIC_IMAGES = ROOT / "static" / "images"
NO_IMAGE = "/static/no-image.png"

UA_WEB = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "image/webp,image/*,*/*;q=0.8",
}

MIN_SIDE = 280
MAX_BYTES = 10 * 1024 * 1024
SLEEP = 1.0

# ─── Hardcoded image map (Wikimedia Commons + Flickr CC) ────────────────────
# Keys are model names (exact, case-sensitive) matching sneakers_db.json.
# All images are open-licensed (CC0, CC-BY, or CC-BY-SA).
# URLs are direct download links — no hotlink protection.
KNOWN_IMAGES: dict[str, str] = {
    # Adidas ─────────────────────────────────────────────────────────────────
    "Samba OG": "https://upload.wikimedia.org/wikipedia/commons/f/fa/Adidas_Samba_OG.jpg",
    "Samba OG Women": "https://upload.wikimedia.org/wikipedia/commons/f/fa/Adidas_Samba_OG.jpg",
    "Samba Classic": "https://upload.wikimedia.org/wikipedia/commons/f/fa/Adidas_Samba_OG.jpg",
    "Gazelle Indoor": "https://upload.wikimedia.org/wikipedia/commons/1/15/Adidas_Gazelle_Indoor.jpg",
    "SL 72 OG Women": "https://upload.wikimedia.org/wikipedia/commons/8/80/Adidas_SL_72_OG.jpg",
    "Stan Smith Recon": "https://upload.wikimedia.org/wikipedia/commons/0/03/Stan_Smith_white_and_green.png",
    # Converse ────────────────────────────────────────────────────────────────
    "Chuck 70 Hi": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/a5/Black_Converse_sneakers.JPG/1280px-Black_Converse_sneakers.JPG",
    "Chuck 70 Women": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/a5/Black_Converse_sneakers.JPG/1280px-Black_Converse_sneakers.JPG",
    "One Star Pro": "https://upload.wikimedia.org/wikipedia/commons/4/42/Converse_One-Star.jpg",
    "One Star Women": "https://upload.wikimedia.org/wikipedia/commons/4/42/Converse_One-Star.jpg",
    "Run Star Hike Hi": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/a5/Black_Converse_sneakers.JPG/1280px-Black_Converse_sneakers.JPG",
    "Run Star Hike Low": "https://upload.wikimedia.org/wikipedia/commons/4/42/Converse_One-Star.jpg",
    "Run Star Hike Women": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/a5/Black_Converse_sneakers.JPG/1280px-Black_Converse_sneakers.JPG",
    # Vans ────────────────────────────────────────────────────────────────────
    "Sk8-Hi Reissue": "https://live.staticflickr.com/4745/40539911132_a19a24fb22_b.jpg",
    "Sk8-Hi Tapered": "https://live.staticflickr.com/4745/40539911132_a19a24fb22_b.jpg",
    "Sk8-Hi Women": "https://live.staticflickr.com/65535/51331633022_df19b0b974_b.jpg",
    "Slip-On Platform": "https://live.staticflickr.com/2664/3978922517_6cc69e4d9e_b.jpg",
    "Slip-On Women": "https://live.staticflickr.com/2664/3978922517_6cc69e4d9e_b.jpg",
    # Puma ────────────────────────────────────────────────────────────────────
    "RS-X Women": "https://upload.wikimedia.org/wikipedia/commons/f/f3/Puma_Suede.jpg",  # RS-X fallback
    "Speedcat": "https://upload.wikimedia.org/wikipedia/commons/1/1f/Puma_Speedcat.jpg",
    "Speedcat OG": "https://upload.wikimedia.org/wikipedia/commons/1/1f/Puma_Speedcat.jpg",
    "Speedcat Women": "https://upload.wikimedia.org/wikipedia/commons/1/1f/Puma_Speedcat.jpg",
    "Suede Classic Archive": "https://upload.wikimedia.org/wikipedia/commons/f/f3/Puma_Suede.jpg",
    "Suede Women": "https://upload.wikimedia.org/wikipedia/commons/f/f3/Puma_Suede.jpg",
    # Reebok ──────────────────────────────────────────────────────────────────
    "Club C 85 Extra": "https://live.staticflickr.com/65535/52600040901_f9fe8b14f2_b.jpg",
    "Freestyle Hi 1987": "https://upload.wikimedia.org/wikipedia/commons/thumb/7/74/Reebok_freestyle_cropped.jpg/1280px-Reebok_freestyle_cropped.jpg",
    # Asics ───────────────────────────────────────────────────────────────────
    "GT-2160 Premium": "https://upload.wikimedia.org/wikipedia/commons/9/94/Asics_GT-2160.jpg",
    "Gel-Kayano 14 Vintage": "https://live.staticflickr.com/65535/54216132793_082da6fd5d_b.jpg",
    # Salomon ─────────────────────────────────────────────────────────────────
    "Pulsar Platform": "https://upload.wikimedia.org/wikipedia/commons/6/6b/Salomon_trail_running_shoes_women_Speedcross.jpg",
    "Speedcross 6": "https://upload.wikimedia.org/wikipedia/commons/6/6b/Salomon_trail_running_shoes_women_Speedcross.jpg",
    "Speedcross 6 GTX": "https://upload.wikimedia.org/wikipedia/commons/6/6b/Salomon_trail_running_shoes_women_Speedcross.jpg",
    "XT-4 Advanced": "https://live.staticflickr.com/65535/52869549200_89a7c785ec_b.jpg",
    "XT-4 OG": "https://live.staticflickr.com/65535/52869549200_89a7c785ec_b.jpg",
    "XT-6 Advanced": "https://live.staticflickr.com/65535/51397988457_c31e758d21_b.jpg",
    "XT-6 Gore-Tex": "https://live.staticflickr.com/65535/51397988457_c31e758d21_b.jpg",
    "XT-6 Women": "https://live.staticflickr.com/65535/51397988457_c31e758d21_b.jpg",
}


# ─── Openverse dynamic search ────────────────────────────────────────────────

def openverse_search(brand: str, model: str, max_results: int = 5) -> list[str]:
    """Search Openverse (CC image search) for the given brand+model."""
    query = f"{brand} {model}"
    try:
        r = requests.get(
            "https://api.openverse.org/v1/images/",
            params={"q": query, "page_size": max_results},
            headers={"User-Agent": "SneakerBot/1.0", "Accept": "application/json"},
            timeout=12,
        )
        if r.status_code != 200:
            return []
        results = r.json().get("results") or []
        return [res["url"] for res in results if res.get("url")]
    except Exception as e:
        print(f"  [openverse] {e}", file=sys.stderr)
        return []


# ─── Image download + validation ─────────────────────────────────────────────

def bad_url(url: str) -> bool:
    u = url.lower()
    return any(x in u for x in ("logo", "favicon", "sprite", "icon", "badge", "banner"))


def download(url: str) -> bytes | None:
    if not url or not url.startswith("http") or bad_url(url):
        return None
    try:
        resp = requests.get(url, headers=UA_WEB, timeout=20, stream=True)
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


def model_slug(model: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (model or "").lower()).strip("-")
    return (s[:120] or "model").strip("-")


# ─── Core per-model processor ────────────────────────────────────────────────

def process(db: dict, model: str, dry: bool, force: bool) -> str:
    entry = db.get(model)
    if not isinstance(entry, dict):
        return "skip: no entry"
    brand = str(entry.get("brand") or "").strip()
    if not brand:
        return "skip: no brand"

    slug = model_slug(model)
    out = STATIC_IMAGES / f"{slug}.jpg"
    rel = f"/static/images/{slug}.jpg"

    cur_img = str(entry.get("image") or "")
    already_local = (
        out.is_file()
        and out.stat().st_size > 0
        and cur_img == rel
        and "no-image" not in cur_img
    )
    if already_local and not force:
        return "⏭  already local"

    if dry:
        mapped = model in KNOWN_IMAGES
        return f"dry-run: {brand} {model} ({'hardcoded' if mapped else 'dynamic'})"

    def try_url(url: str, source: str) -> bool:
        raw = download(url)
        if raw and validate_and_save(raw, out):
            entry["image"] = rel
            entry["source_url"] = url
            entry["source_title"] = f"{brand} {model} — {source}"
            entry["verified"] = False  # Not from official brand/StockX
            return True
        return False

    # ── Strategy 1: Hardcoded known image ────────────────────────────────────
    if model in KNOWN_IMAGES:
        url = KNOWN_IMAGES[model]
        if try_url(url, "wikimedia/flickr-cc"):
            return f"✔ [cc] {rel}"

    # ── Strategy 2: Dynamic Openverse search ─────────────────────────────────
    for url in openverse_search(brand, model):
        if try_url(url, f"openverse"):
            return f"✔ [openverse] {rel}"
        time.sleep(0.2)

    # ── Fail ─────────────────────────────────────────────────────────────────
    entry["verified"] = False
    if not already_local:
        entry["image"] = NO_IMAGE
    return "❌ no valid image found"


# ─── Main ─────────────────────────────────────────────────────────────────

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
        # Only process models missing images
        models = sorted(
            (k for k, v in db.items()
             if isinstance(v, dict) and (
                 args.force
                 or not v.get("image")
                 or "no-image" in str(v.get("image", ""))
             )),
            key=str.lower,
        )

    print(f"Target: {len(models)} models missing images\n")
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
            time.sleep(SLEEP)

    if not args.dry_run:
        DB_PATH.write_text(json.dumps(db, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    verified = sum(1 for v in db.values() if isinstance(v, dict) and v.get("verified"))
    total = len(db)
    print(f"\nSummary: ✔={ok}, ⏭={skip}, ❌={fail}")
    print(f"✅ verified (official sources) in DB: {verified}/{total}")
    print(f"📸 with any local image: {sum(1 for v in db.values() if isinstance(v, dict) and v.get('image') and 'no-image' not in str(v.get('image','')) and str(v.get('image','')).startswith('/static/images/'))}/{total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
