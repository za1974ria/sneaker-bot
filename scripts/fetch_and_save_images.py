#!/usr/bin/env python3
"""
Télécharge une image locale par modèle (sneakers_db.json) via SerpAPI Google Images.

Validation stricte : une image n'est acceptée que si le titre (SerpAPI) contient
marque + modèle + référence (voir ``is_valid_product``). Sinon : pas de téléchargement,
``verified: false``, image de secours.

Champs JSON enrichis / conservés : brand, ref, image, source_url, source_title, verified.

Usage (SERPAPI_KEY dans .env) :
  ./venv/bin/python scripts/fetch_and_save_images.py
  ./venv/bin/python scripts/fetch_and_save_images.py --force
  ./venv/bin/python scripts/fetch_and_save_images.py --dry-run

Audit :
  ./venv/bin/python scripts/audit_products.py
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
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
STATIC_IMAGES = ROOT / "static" / "images"
SNEAKERS_DB = ROOT / "static" / "sneakers_db.json"
MODELS_LIST = ROOT / "data" / "models_list.json"
NO_IMAGE = "/static/no-image.png"
NO_IMAGE_PATH = ROOT / "static" / "no-image.png"

DOMAIN_PRIORITY = [
    "secure-images.nike.com",
    "nike.com",
    "adidas.com",
    "assets.adidas.com",
    "reebok.com",
    "puma.com",
    "images.puma.com",
    "newbalance.com",
    "nb.com",
    "images.stockx.com",
    "stockx.com",
]

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}

MIN_SIDE = 200
MAX_BYTES = 6 * 1024 * 1024
SERP_SLEEP_SEC = 1.2


def load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except Exception:
        pass


def is_valid_product(source_title: str, brand: str, model: str, ref: str) -> bool:
    if not source_title:
        return False

    title = source_title.lower()

    return (
        brand.lower() in title
        and model.lower() in title
        and ref.lower() in title
    )


def model_slug(model: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (model or "").lower()).strip("-")
    return (s[:120] or "model").strip("-")


def domain_rank(url: str) -> int:
    host = (urlparse(url).hostname or "").lower()
    for i, d in enumerate(DOMAIN_PRIORITY):
        if d in host:
            return i
    return len(DOMAIN_PRIORITY) + 10


def bad_url_hint(url: str) -> bool:
    u = url.lower()
    if any(x in u for x in ("logo", "favicon", "sprite", "icon-", "/icons/", "badge", "pixel", "1x1")):
        return True
    return False


def serpapi_image_candidates(query: str, api_key: str, num: int = 40) -> list[dict]:
    r = requests.get(
        "https://serpapi.com/search.json",
        params={
            "engine": "google_images",
            "q": query,
            "api_key": api_key,
            "num": num,
            "safe": "active",
        },
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    return list(data.get("images_results") or [])


def score_result(item: dict) -> tuple[int, int]:
    url = str(item.get("original") or item.get("link") or "")
    w = int(item.get("original_width") or item.get("width") or 0)
    h = int(item.get("original_height") or item.get("height") or 0)
    dr = domain_rank(url)
    min_side = min(w, h) if w and h else 0
    return (dr, -min_side)


def download_bytes(url: str) -> bytes | None:
    if bad_url_hint(url):
        return None
    try:
        resp = requests.get(url, headers=UA, timeout=25, stream=True)
        if resp.status_code != 200:
            return None
        buf = io.BytesIO()
        n = 0
        for chunk in resp.iter_content(65536):
            if not chunk:
                continue
            n += len(chunk)
            if n > MAX_BYTES:
                return None
            buf.write(chunk)
        return buf.getvalue()
    except Exception:
        return None


def validate_and_save_jpeg(data: bytes, out: Path) -> bool:
    try:
        from PIL import Image
    except ImportError:
        print("❌ Installez Pillow: pip install Pillow", file=sys.stderr)
        return False
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
        w, h = im.size
        if min(w, h) < MIN_SIDE:
            return False
        if max(w, h) / max(min(w, h), 1) > 4.5:
            return False
        if im.mode == "RGBA":
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[3])
            im = bg
        elif im.mode != "RGB":
            im = im.convert("RGB")
        out.parent.mkdir(parents=True, exist_ok=True)
        im.save(out, format="JPEG", quality=88, optimize=True)
        return True
    except Exception:
        return False


def _ensure_schema(entry: dict, brand: str) -> None:
    if str(entry.get("brand") or "").strip() == "" and brand:
        entry["brand"] = brand
    entry.setdefault("ref", str(entry.get("ref") or ""))
    entry.setdefault("image", str(entry.get("image") or ""))
    entry.setdefault("source_url", str(entry.get("source_url") or ""))
    entry.setdefault("source_title", str(entry.get("source_title") or ""))
    entry.setdefault("verified", bool(entry.get("verified", False)))


def main() -> int:
    load_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Re-télécharger même si la cible existe déjà")
    parser.add_argument("--dry-run", action="store_true", help="Ne pas écrire fichiers ni JSON")
    parser.add_argument("--limit", type=int, default=0, help="Traiter au plus N modèles (0 = tous)")
    args = parser.parse_args()

    api_key = (os.getenv("SERPAPI_KEY") or "").strip()
    if not api_key and not args.dry_run:
        print("⚠  SERPAPI_KEY absent — aucune recherche SerpAPI possible.")

    STATIC_IMAGES.mkdir(parents=True, exist_ok=True)
    if not NO_IMAGE_PATH.is_file():
        print("❌ Fichier manquant:", NO_IMAGE_PATH, file=sys.stderr)
        return 1

    brand_by_model: dict[str, str] = {}
    if MODELS_LIST.is_file():
        cat = json.loads(MODELS_LIST.read_text(encoding="utf-8"))
        for row in cat:
            m, b = row.get("model"), row.get("brand")
            if m and b:
                brand_by_model[str(m)] = str(b)

    db = json.loads(SNEAKERS_DB.read_text(encoding="utf-8"))
    if not isinstance(db, dict):
        print("❌ sneakers_db.json invalide", file=sys.stderr)
        return 1

    models = sorted(db.keys(), key=lambda x: x.lower())
    if args.limit and args.limit > 0:
        models = models[: args.limit]
    changed = 0

    for model in models:
        entry = db[model]
        if not isinstance(entry, dict):
            continue
        brand = brand_by_model.get(model, "")
        _ensure_schema(entry, brand)
        slug = model_slug(model)
        rel = f"/static/images/{slug}.jpg"
        out = STATIC_IMAGES / f"{slug}.jpg"

        if (
            not args.force
            and out.is_file()
            and str(entry.get("image") or "").strip() == rel
        ):
            print(f"⏭  {model} (déjà local: {rel})")
            continue

        query = f"{brand} {model} sneaker official product".strip()

        if args.dry_run:
            print(f"…  [dry-run] {model} → {query}")
            continue

        ref_str = str(entry.get("ref") or "").strip()
        items: list[dict] = []
        if api_key:
            try:
                items = serpapi_image_candidates(query, api_key)
                items.sort(key=score_result)
            except Exception as exc:
                print(f"⚠  SerpAPI {model}: {exc}")

        ok_download = False
        chosen_title = ""
        chosen_url = ""

        for it in items:
            u = str(it.get("original") or it.get("link") or "").strip()
            if not u.startswith("http"):
                continue
            w = int(it.get("original_width") or it.get("width") or 0)
            h = int(it.get("original_height") or it.get("height") or 0)
            if w and h and min(w, h) < MIN_SIDE:
                continue
            source_title = str(it.get("title") or it.get("source") or "")
            if not is_valid_product(source_title, brand, model, ref_str):
                continue
            raw = download_bytes(u)
            if not raw or not validate_and_save_jpeg(raw, out):
                continue
            ok_download = True
            chosen_title = source_title[:500]
            chosen_url = u
            break

        if ok_download:
            entry["image"] = rel
            entry["source_title"] = chosen_title
            entry["source_url"] = chosen_url
            entry["verified"] = True
            changed += 1
            print(f"✔  {model} → {rel}")
        else:
            _ensure_schema(entry, brand)
            entry["verified"] = False
            entry["source_url"] = ""
            entry["source_title"] = ""
            cur_img = str(entry.get("image") or "").strip()
            local_disk = ROOT / cur_img.lstrip("/") if cur_img.startswith("/") else Path()
            if (
                cur_img.startswith("/static/images/")
                and "no-image" not in cur_img.lower()
                and local_disk.is_file()
            ):
                pass
            else:
                entry["image"] = NO_IMAGE
            changed += 1
            print(f"❌ REJECTED (mismatch): {model}")

        time.sleep(SERP_SLEEP_SEC)

    if not args.dry_run:
        SNEAKERS_DB.write_text(json.dumps(db, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\nTerminé — entrées modifiées: {changed}, JSON: {SNEAKERS_DB}")

    total = len(db)
    verified_count = sum(1 for p in db.values() if isinstance(p, dict) and p.get("verified"))
    print(f"✅ VERIFIED: {verified_count}/{total}")
    if args.dry_run:
        print("\n[dry-run] aucune écriture fichier/JSON")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
