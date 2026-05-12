#!/usr/bin/env python3
"""
Pipeline images sneakers — validation en 3 niveaux par confiance décroissante.

Niveau 1 (verified=True, confiance maximale) :
  - Source officielle de la marque (nike.com, adidas.com, …) OU StockX
  - Référence SKU confirmée dans titre + source + URL

Niveau 2 (verified=True, confiance correcte) :
  - Source officielle OU StockX
  - Marque + modèle dans le titre (SKU absent/non trouvé mais source fiable)

Niveau 3 (verified=False, confiance basse) :
  - N'importe quelle source si marque + modèle dans le titre
  - Stocké localement, non marqué verified — sert de meilleure image provisoire

Fallback : image=/static/no-image.png, verified=false.

Usage :
  ./venv/bin/python scripts/fetch_verified_images.py --model "Air Jordan 1 Mid"
  ./venv/bin/python scripts/fetch_verified_images.py --limit 5
  ./venv/bin/python scripts/fetch_verified_images.py --all
  ./venv/bin/python scripts/fetch_verified_images.py --all --force   # re-dl même si déjà local
"""
from __future__ import annotations

import argparse
import io
import itertools
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

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
TRUSTED_DOMAINS = (
    "sneakernews.com",
    "hypebeast.com",
    "sneakers.fr",
    "solecollector.com",
    "kicksonfire.com",
    "sneakerfreaker.com",
    "highsnobiety.com",
    "footlocker.fr",
    "foot-locker.fr",
    "jdsports.fr",
    "zalando.fr",
    "farfetch.com",
)

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}
MIN_SIDE = 280
MAX_BYTES = 8 * 1024 * 1024
SLEEP = 3.0
_counter = itertools.count()


def load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except Exception:
        pass


def model_slug(model: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (model or "").lower()).strip("-")
    return (s[:120] or "model").strip("-")


def ref_in_text(ref: str, text: str) -> bool:
    """Vérifie si la référence SKU apparaît dans le texte (insensible à la casse, tirets/underscores normalisés)."""
    if not ref or not text:
        return False
    r1 = ref.upper().replace("_", "-").strip()
    t = text.upper().replace("_", "-")
    r2 = re.sub(r"[^A-Z0-9]", "", ref.upper())
    t2 = re.sub(r"[^A-Z0-9]", "", text.upper())
    if len(r2) < 4:
        return False
    return r1 in t or r2 in t2


def model_in_title(brand: str, model: str, title: str) -> bool:
    """Vérifie que la marque ET le modèle apparaissent dans le titre."""
    if not title:
        return False
    t = title.lower()
    # Split model en mots-clés significatifs (ignore les mots trop courts)
    keywords = [w for w in model.lower().split() if len(w) > 1]
    # Au moins 70% des mots-clés du modèle doivent être présents
    if not keywords:
        return False
    matched = sum(1 for kw in keywords if kw in t)
    ratio = matched / len(keywords)
    return brand.lower() in t and ratio >= 0.7


def host_kind(url: str) -> str:
    """Match by exact hostname or subdomain only (prevents substring false positives)."""
    h = (urlparse(url).hostname or "").lower()
    for d in OFFICIAL_DOMAINS:
        if h == d or h.endswith("." + d):
            return "official"
    for d in STOCKX_DOMAINS:
        if h == d or h.endswith("." + d):
            return "stockx"
    for d in TRUSTED_DOMAINS:
        if h == d or h.endswith("." + d):
            return "trusted"
    return "other"


def combined_text(it: dict) -> str:
    return " | ".join([
        str(it.get("title") or ""),
        str(it.get("source") or ""),
        str(it.get("link") or ""),
        str(it.get("original") or ""),
    ])


def bad_url(url: str) -> bool:
    u = url.lower()
    return any(x in u for x in ("logo", "favicon", "sprite", "/icon", "badge", "1x1", "collage", "banner"))


def serp_items(query: str, api_key: str) -> list[dict]:
    r = requests.get(
        "https://serpapi.com/search.json",
        params={"engine": "google_images", "q": query, "api_key": api_key, "num": 40},
        timeout=35,
    )
    r.raise_for_status()
    return list(r.json().get("images_results") or [])


def download(url: str) -> bytes | None:
    if bad_url(url) or not url.startswith("http"):
        return None
    try:
        resp = requests.get(url, headers=UA, timeout=25, stream=True)
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


def validate_jpeg(data: bytes, out: Path) -> bool:
    try:
        from PIL import Image
    except ImportError:
        print("❌ Pillow manquant: pip install Pillow", file=sys.stderr)
        return False
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
        w, h = im.size
        if min(w, h) < MIN_SIDE:
            return False
        if max(w, h) / max(min(w, h), 1) > 3.0:
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


def pick_candidate(
    items: list[dict],
    brand: str,
    model: str,
    ref: str,
) -> tuple[dict | None, int, str]:
    """
    Retourne (item, confidence_level, raison).
    confidence_level : 1=strict(officiel+ref), 2=officiel sans ref, 3=trusted+title, 0=échec
    """
    tier1: list[tuple[int, int, int, dict]] = []  # (pri, -min_side, seq, item)
    tier2: list[tuple[int, int, int, dict]] = []
    tier3: list[tuple[int, int, int, dict]] = []

    for it in items:
        url = str(it.get("original") or it.get("link") or "").strip()
        if not url.startswith("http") or bad_url(url):
            continue
        kind = host_kind(url)
        title = str(it.get("title") or "")
        blob = combined_text(it)
        w = int(it.get("original_width") or it.get("width") or 0)
        h = int(it.get("original_height") or it.get("height") or 0)
        min_side = min(w, h) if w and h else 0
        seq = next(_counter)
        pri = {"official": 0, "stockx": 1, "trusted": 2, "other": 3}[kind]

        if kind in ("official", "stockx") and ref_in_text(ref, blob):
            tier1.append((pri, -min_side, seq, it))
        elif kind in ("official", "stockx") and model_in_title(brand, model, title):
            tier2.append((pri, -min_side, seq, it))
        elif kind in ("trusted", "other") and model_in_title(brand, model, title):
            tier3.append((0, -min_side, seq, it))

    if tier1:
        tier1.sort(key=lambda x: x[:3])
        return tier1[0][3], 1, f"officiel/stockx + ref confirmée"
    if tier2:
        tier2.sort(key=lambda x: x[:3])
        return tier2[0][3], 2, f"officiel/stockx, marque+modèle dans titre"
    if tier3:
        tier3.sort(key=lambda x: x[:3])
        return tier3[0][3], 3, f"source tierce, marque+modèle dans titre"
    return None, 0, "aucun résultat valide"


def _set_ok(entry: dict, rel: str, url: str, title: str, level: int) -> None:
    entry["image"] = rel
    entry["source_url"] = url
    entry["source_title"] = title[:500]
    entry["verified"] = level <= 2  # True pour niveau 1 et 2


def _fail(entry: dict, reason: str) -> None:
    # Ne réinitialise pas une image locale déjà valide
    cur = str(entry.get("image") or "")
    if cur.startswith("/static/images/") and "no-image" not in cur:
        local = ROOT / "static" / cur[len("/static/"):]
        if local.is_file() and local.stat().st_size > 0:
            entry["verified"] = False
            entry["source_title"] = ("REJECT: " + reason)[:300]
            return
    entry["image"] = NO_IMAGE
    entry["source_url"] = ""
    entry["source_title"] = ("REJECT: " + reason)[:300]
    entry["verified"] = False


def process_one(db: dict, model: str, api_key: str, dry: bool, force: bool) -> str:
    entry = db.get(model)
    if not isinstance(entry, dict):
        return "skip: entrée absente"
    brand = str(entry.get("brand") or "").strip()
    ref = str(entry.get("ref") or "").strip()
    if not brand:
        return "skip: brand vide"

    slug = model_slug(model)
    out = STATIC_IMAGES / f"{slug}.jpg"
    rel = f"/static/images/{slug}.jpg"

    # Skip si déjà local + verified et pas --force
    if (
        not force
        and out.is_file()
        and out.stat().st_size > 0
        and entry.get("verified") is True
        and str(entry.get("image") or "") == rel
    ):
        return f"⏭  déjà vérifié ({rel})"

    # Requête enrichie avec ref si disponible
    query = f"{brand} {model} {ref} sneaker product".strip() if ref else f"{brand} {model} sneaker official product"

    if dry:
        return f"dry-run: {query}"

    try:
        items = serp_items(query, api_key)
    except Exception as e:
        _fail(entry, f"serpapi: {e}")
        return f"serpapi error: {e}"

    picked, level, why = pick_candidate(items, brand, model, ref)
    if not picked:
        _fail(entry, why)
        return f"❌ {why}"

    url = str(picked.get("original") or picked.get("link") or "")
    raw = download(url)
    if not raw:
        _fail(entry, "téléchargement impossible")
        return "❌ download fail"

    if not validate_jpeg(raw, out):
        _fail(entry, "image rejetée (taille/format)")
        return "❌ validate fail"

    title_txt = str(picked.get("title") or picked.get("source") or "")
    _set_ok(entry, rel, url, title_txt, level)
    verified_tag = "verified" if level <= 2 else "low-conf"
    return f"✔ [{verified_tag} L{level}] {rel}"


def main() -> int:
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", type=str, default="", help="Un modèle exact (clé JSON)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="Re-télécharger même si déjà vérifié")
    args = ap.parse_args()

    api_key = (os.getenv("SERPAPI_KEY") or "").strip()
    if not api_key and not args.dry_run:
        print("SERPAPI_KEY manquant", file=sys.stderr)
        return 1

    db = json.loads(DB_PATH.read_text(encoding="utf-8"))
    if not isinstance(db, dict):
        return 1

    STATIC_IMAGES.mkdir(parents=True, exist_ok=True)

    models = sorted(db.keys(), key=lambda x: x.lower())
    if args.model:
        models = [args.model] if args.model in db else []
        if not models:
            print("Modèle inconnu:", args.model, file=sys.stderr)
            return 1
    elif args.limit > 0:
        models = models[: args.limit]
    elif not args.all:
        print("Précisez --model <nom>, --limit N ou --all", file=sys.stderr)
        return 1

    ok = rej = skip = 0
    for m in models:
        r = process_one(db, m, api_key, args.dry_run, args.force)
        print(m, "→", r, flush=True)
        if r.startswith("✔"):
            ok += 1
        elif r.startswith("⏭"):
            skip += 1
        elif not r.startswith("dry-run"):
            rej += 1
        if not args.dry_run:
            time.sleep(SLEEP)

    if not args.dry_run:
        DB_PATH.write_text(json.dumps(db, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    verified = sum(1 for v in db.values() if isinstance(v, dict) and v.get("verified"))
    total = len(db)
    print(f"\nRésumé: traités={len(models)}, ✔={ok}, ⏭={skip}, ❌={rej}")
    print(f"✅ VERIFIED dans DB: {verified}/{total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
