#!/usr/bin/env python3
"""
Scrape official product images via Playwright from brand websites.
Sets verified=True for successfully fetched official images.

Brands covered:
  Adidas   → adidas.fr
  Puma     → fr.puma.com
  Vans     → vans.fr
  Converse → converse.com/fr
  Salomon  → salomon.com/fr-fr
  On Running → on.com/fr-fr

Usage:
  ./venv/bin/python scripts/fetch_official_images.py
  ./venv/bin/python scripts/fetch_official_images.py --brand Adidas
  ./venv/bin/python scripts/fetch_official_images.py --brand Salomon --force
  ./venv/bin/python scripts/fetch_official_images.py --dry-run
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus, urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "static" / "sneakers_db.json"
STATIC_IMAGES = ROOT / "static" / "images"
NO_IMAGE = "/static/no-image.png"

CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--disable-background-networking",
    "--mute-audio",
    "--no-first-run",
]

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

MIN_SIDE = 280
MAX_BYTES = 12 * 1024 * 1024
OFFICIAL_DOMAINS = (
    "assets.adidas.com", "adidas.fr", "adidas.com",
    "images.puma.com", "fr.puma.com", "puma.com",
    "images.vans.com", "vans.fr", "vans.com",
    "converse.com",
    "salomon.com",
    "on.com", "on-running.com",
)


def model_slug(model: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (model or "").lower()).strip("-")
    return (s[:120] or "model").strip("-")


def is_official_url(url: str) -> bool:
    h = (urlparse(url).hostname or "").lower()
    return any(d in h for d in OFFICIAL_DOMAINS)


# ─── Brand URL builders ──────────────────────────────────────────────────────

def adidas_urls(model: str, ref: str) -> list[str]:
    slug = model_slug(f"adidas {model}")
    return [
        f"https://www.adidas.fr/{slug}/{ref}.html",
        f"https://www.adidas.com/us/{slug}/{ref}.html",
        f"https://www.adidas.fr/search?q={quote_plus(ref)}",
    ]

def puma_urls(model: str, ref: str) -> list[str]:
    slug = model_slug(f"puma {model}")
    num = ref.split("-")[0]
    return [
        f"https://fr.puma.com/fr/fr/pd/{slug}/{ref}.html",
        f"https://fr.puma.com/fr/fr/pd/{num}.html",
        f"https://us.puma.com/en/us/pd/{slug}/{ref}.html",
        f"https://fr.puma.com/fr/fr/recherche?q={quote_plus(ref)}",
    ]

def vans_urls(model: str, ref: str) -> list[str]:
    return [
        f"https://www.vans.fr/en/products/{ref.lower()}",
        f"https://www.vans.com/en-us/products/{ref.lower()}",
        f"https://www.vans.fr/en/search?q={quote_plus(ref)}",
    ]

def converse_urls(model: str, ref: str) -> list[str]:
    slug = model_slug(model)
    return [
        f"https://www.converse.com/fr/shop/p/{slug}/{ref}",
        f"https://www.converse.com/shop/p/{slug}/{ref}",
        f"https://www.converse.com/fr/shop/p/{ref}",
        f"https://www.converse.com/fr/search?q={quote_plus(ref)}",
    ]

def salomon_urls(model: str, ref: str) -> list[str]:
    slug = model_slug(model)
    return [
        f"https://www.salomon.com/fr-fr/shop-emea/product/{slug}/{ref}.html",
        f"https://www.salomon.com/en-us/shop-amr/product/{slug}/{ref}.html",
        f"https://www.salomon.com/fr-fr/search?Ntt={quote_plus(ref)}",
    ]

def on_running_urls(model: str, ref: str) -> list[str]:
    slug = model_slug(model)
    return [
        f"https://www.on.com/fr-fr/shop/products/{slug}-{ref}.html",
        f"https://www.on.com/fr-fr/search?q={quote_plus(model)}",
        f"https://www.on.com/fr-fr/shop/",
    ]

BRAND_URL_BUILDERS = {
    "Adidas": adidas_urls,
    "Puma": puma_urls,
    "Vans": vans_urls,
    "Converse": converse_urls,
    "Salomon": salomon_urls,
    "On Running": on_running_urls,
}


# ─── Image extraction helpers ────────────────────────────────────────────────

def extract_og_image(page) -> Optional[str]:
    try:
        el = page.locator('meta[property="og:image"]').first
        if el.count():
            return el.get_attribute("content", timeout=2000)
    except Exception:
        pass
    return None


def extract_next_data_image(page, brand: str, model: str) -> Optional[str]:
    """Extract image from __NEXT_DATA__ (Next.js SSR pages)."""
    try:
        raw = page.evaluate("JSON.stringify(window.__NEXT_DATA__ || null)")
        if not raw or raw == "null":
            return None
        data = json.loads(raw)
        text = json.dumps(data)
        # Look for CDN image URLs
        patterns = [
            r'"(https://assets\.adidas\.com/images/[^"]+\.jpg)"',
            r'"(https://images\.puma\.com/image/upload/[^"]+\.(?:jpg|png))"',
            r'"(https://images\.vans\.com/[^"]+\.(?:jpg|png))"',
            r'"(https://[^"]*converse[^"]+\.(?:jpg|png))"',
            r'"(https://[^"]*salomon[^"]+\.(?:jpg|png))"',
        ]
        for pat in patterns:
            m = re.search(pat, text)
            if m:
                return m.group(1)
    except Exception:
        pass
    return None


def extract_product_image_from_dom(page, brand: str) -> Optional[str]:
    """Try various DOM selectors for product images."""
    selectors = []
    b = brand.lower()

    if b == "adidas":
        selectors = [
            "[data-auto-id='product-image'] img",
            ".carousel__image img",
            "img[class*='product'][class*='image']",
            "img[class*='ProductImage']",
        ]
    elif b == "puma":
        selectors = [
            ".pdp-link img",
            "[class*='ProductMainImage'] img",
            "[data-test='product-image'] img",
        ]
    elif b == "vans":
        selectors = [
            "[class*='product-image'] img",
            "[class*='ProductImage'] img",
            "img[data-src*='vans']",
        ]
    elif b == "converse":
        selectors = [
            "[class*='product-image'] img",
            "[class*='hero'] img",
        ]
    elif b == "salomon":
        selectors = [
            "[class*='gallery'] img",
            "[class*='product-image'] img",
        ]
    elif b == "on running":
        selectors = [
            "[class*='ProductImage'] img",
            "[class*='product-image'] img",
        ]

    for sel in selectors:
        try:
            el = page.locator(sel).first
            if el.count():
                for attr in ("src", "data-src", "data-lazy-src"):
                    v = el.get_attribute(attr, timeout=1500)
                    if v and v.startswith("http"):
                        return v
        except Exception:
            continue
    return None


# ─── Image download + validate ───────────────────────────────────────────────

def bad_url(url: str) -> bool:
    u = url.lower()
    return any(x in u for x in ("logo", "favicon", "sprite", "icon", "badge", "banner", "1x1"))


def download_and_validate(url: str, out: Path) -> bool:
    if not url or not url.startswith("http") or bad_url(url):
        return False
    try:
        headers = {
            "User-Agent": DESKTOP_UA,
            "Accept": "image/webp,image/*,*/*;q=0.8",
            "Referer": f"https://{urlparse(url).hostname}/",
        }
        resp = requests.get(url, headers=headers, timeout=25, stream=True)
        if resp.status_code != 200:
            return False
        buf = io.BytesIO()
        n = 0
        for ch in resp.iter_content(65536):
            n += len(ch)
            if n > MAX_BYTES:
                return False
            buf.write(ch)
        data = buf.getvalue()
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        im.load()
        w, h = im.size
        if min(w, h) < MIN_SIDE:
            return False
        if max(w, h) / max(min(w, h), 1) > 3.5:
            return False
        if im.mode == "RGBA":
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[3])
            im = bg
        elif im.mode != "RGB":
            im = im.convert("RGB")
        out.parent.mkdir(parents=True, exist_ok=True)
        im.save(out, "JPEG", quality=92, optimize=True)
        return True
    except Exception:
        return False


# ─── Per-brand Playwright page scraper ──────────────────────────────────────

def scrape_brand_page(page, brand: str, model: str, ref: str) -> Optional[str]:
    """
    Navigate brand product pages and extract the best product image URL.
    Returns an image URL or None.
    """
    url_builder = BRAND_URL_BUILDERS.get(brand)
    if not url_builder:
        return None

    urls = url_builder(model, ref)
    b = brand.lower()

    for url in urls[:3]:
        try:
            print(f"    → {url[:80]}", flush=True)
            page.goto(url, wait_until="domcontentloaded", timeout=28000)

            # Wait a moment for SSR-injected meta tags and lazy images
            time.sleep(1.5)

            # 1. og:image (most reliable — set server-side in SSR pages)
            img_url = extract_og_image(page)
            if img_url and is_official_url(img_url):
                print(f"    ✓ og:image: {img_url[:80]}", flush=True)
                return img_url

            # 2. __NEXT_DATA__ (Next.js SSR payload)
            img_url = extract_next_data_image(page, brand, model)
            if img_url:
                print(f"    ✓ __NEXT_DATA__: {img_url[:80]}", flush=True)
                return img_url

            # 3. DOM image selectors
            img_url = extract_product_image_from_dom(page, brand)
            if img_url and is_official_url(img_url):
                print(f"    ✓ DOM: {img_url[:80]}", flush=True)
                return img_url

            # 4. Search result page — find product link and follow it
            if "search" in url or "recherche" in url:
                img_url = _follow_first_search_result(page, brand, model, ref)
                if img_url:
                    return img_url

        except Exception as e:
            err = str(e)[:80]
            print(f"    ✗ {url[:60]}: {err}", flush=True)
            continue

    return None


def _follow_first_search_result(page, brand: str, model: str, ref: str) -> Optional[str]:
    """On a search results page, click the first product and get its og:image."""
    try:
        # Wait for search results
        page.wait_for_selector("a[href*='/shop'], a[href*='/product'], a[href*='/pd/']",
                               timeout=10000)
        links = page.locator("a[href*='/shop'], a[href*='/product'], a[href*='/pd/']").all()
        for link in links[:3]:
            href = link.get_attribute("href") or ""
            if ref.lower() in href.lower() or model_slug(model) in href.lower():
                link.click()
                time.sleep(2)
                img_url = extract_og_image(page)
                if img_url and is_official_url(img_url):
                    return img_url
    except Exception:
        pass
    return None


# ─── Main processor ──────────────────────────────────────────────────────────

def process_brand(db: dict, brand: str, force: bool, dry: bool) -> dict[str, str]:
    """Process all unverified models for a brand. Returns {model: result_string}."""
    from playwright.sync_api import sync_playwright

    models = [
        (m, e.get("ref", ""))
        for m, e in db.items()
        if isinstance(e, dict)
        and e.get("brand") == brand
        and (force or not e.get("verified", False))
    ]

    if not models:
        return {}

    print(f"\n{'='*60}")
    print(f"Brand: {brand} — {len(models)} models")
    print('='*60)

    results = {}

    if dry:
        for model, ref in models:
            results[model] = f"dry-run: {brand} {model} ({ref})"
        return results

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=CHROMIUM_ARGS)
        context = browser.new_context(
            user_agent=IPHONE_UA if brand in ("Vans", "Puma") else DESKTOP_UA,
            viewport={"width": 1280, "height": 900},
            locale="fr-FR",
            timezone_id="Europe/Paris",
            extra_http_headers={
                "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )

        # Block analytics/ads to speed up loading
        context.route(
            re.compile(r"(google-analytics|doubleclick|googletagmanager|facebook|hotjar|optimizely)"),
            lambda route: route.abort(),
        )

        page = context.new_page()

        for model, ref in models:
            slug = model_slug(model)
            out = STATIC_IMAGES / f"{slug}.jpg"
            rel = f"/static/images/{slug}.jpg"
            entry = db[model]

            # Skip if already local & verified (unless --force)
            if not force and out.is_file() and out.stat().st_size > 0 and entry.get("verified"):
                results[model] = "⏭  already verified"
                continue

            print(f"\n[{brand}] {model} ({ref})", flush=True)

            img_url = scrape_brand_page(page, brand, model, ref)

            if img_url and download_and_validate(img_url, out):
                entry["image"] = rel
                entry["source_url"] = img_url
                entry["source_title"] = f"{brand} {model} — official"
                entry["verified"] = True
                results[model] = f"✔ [official] {img_url[:70]}"
                print(f"  ✅ saved {out.name}", flush=True)
            else:
                reason = f"no image extracted" if not img_url else f"download/validate failed: {img_url[:60]}"
                results[model] = f"❌ {reason}"
                print(f"  ❌ {reason}", flush=True)

            time.sleep(2.0)

        page.close()
        context.close()
        browser.close()

    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--brand", type=str, default="",
                    help="Process one brand (Adidas, Puma, Vans, Converse, Salomon, On Running)")
    ap.add_argument("--force", action="store_true",
                    help="Re-fetch even if already verified")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db: dict = json.loads(DB_PATH.read_text(encoding="utf-8"))
    STATIC_IMAGES.mkdir(parents=True, exist_ok=True)

    target_brands = (
        [args.brand] if args.brand
        else list(BRAND_URL_BUILDERS.keys())
    )

    all_results: dict[str, str] = {}
    ok = fail = skip = 0

    for brand in target_brands:
        res = process_brand(db, brand, args.force, args.dry_run)
        all_results.update(res)
        for model, r in res.items():
            if r.startswith("✔"):
                ok += 1
            elif r.startswith("⏭"):
                skip += 1
            elif not r.startswith("dry"):
                fail += 1

    if not args.dry_run:
        DB_PATH.write_text(json.dumps(db, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    verified = sum(1 for v in db.values() if isinstance(v, dict) and v.get("verified"))
    total = len(db)

    print(f"\n{'='*60}")
    print(f"Summary: ✔={ok}  ⏭={skip}  ❌={fail}")
    print(f"✅ VERIFIED (official): {verified}/{total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
