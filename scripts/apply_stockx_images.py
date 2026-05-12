#!/usr/bin/env python3
"""Met à jour static/sneakers_db.json : URLs images.stockx.com vérifiées (HEAD 200) + Nike/local inchangés."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CAT = json.loads((ROOT / "data" / "models_list.json").read_text(encoding="utf-8"))
BRAND_BY_MODEL = {row["model"]: row["brand"] for row in CAT if row.get("model") and row.get("brand")}

# URLs vérifiées HTTP 200 depuis ce serveur (images.stockx.com)
SX = {
    "superstar_core": "https://images.stockx.com/images/adidas-Superstar-Core-Black-Cloud-White-Product.jpg",
    "superstar_xlg": "https://images.stockx.com/images/adidas-Superstar-XLG-White-Black-Product.jpg",
    "campus_00s": "https://images.stockx.com/images/adidas-Campus-00s-Amber-Tint-Product.jpg",
    "nb_574_core": "https://images.stockx.com/images/New-Balance-574-Core-Product.jpg",
    "nb_574_legacy": "https://images.stockx.com/images/New-Balance-574-Legacy-Green-Product.jpg",
    "nb_550_wg": "https://images.stockx.com/images/New-Balance-550-White-Green-Product.jpg",
    "nb_990v6": "https://images.stockx.com/images/New-Balance-990v6-Grey-Product.jpg",
    "nb_2002r_rain": "https://images.stockx.com/images/New-Balance-2002R-Rain-Cloud-Product.jpg",
    "puma_suede_xxi": "https://images.stockx.com/images/Puma-Suede-Classic-XXI-Black-White-Product.jpg",
    "reebok_classic_white": "https://images.stockx.com/images/Reebok-Classic-Leather-White-Product.jpg",
    "vans_old_skool": "https://images.stockx.com/images/Vans-Old-Skool-Black-White-Product.jpg",
}


def pick_stockx(model: str, brand: str) -> str:
    m = model.lower()
    b = brand.lower()

    if "superstar xlg" in m:
        return SX["superstar_xlg"]
    if "superstar" in m:
        return SX["superstar_core"]
    if "campus" in m:
        return SX["campus_00s"]
    if "forum" in m:
        return SX["campus_00s"]
    if "samba" in m or "stan smith" in m or "gazelle" in m or "handball" in m or "sl 72" in m:
        return SX["campus_00s"]
    if brand == "New Balance":
        if "574" in m and "legacy" in m:
            return SX["nb_574_legacy"]
        if "574" in m:
            return SX["nb_574_core"]
        if "550" in m:
            return SX["nb_550_wg"]
        if "990" in m:
            return SX["nb_990v6"]
        if "2002" in m:
            return SX["nb_2002r_rain"]
        if "1906" in m or "530" in m or "327" in m or "9060" in m:
            return SX["nb_990v6"]
        return SX["nb_574_core"]
    if brand == "Puma":
        if "suede" in m:
            return SX["puma_suede_xxi"]
        if "rs-x" in m or "rs x" in m:
            return SX["puma_suede_xxi"]
        if "speedcat" in m or "palermo" in m or "clyde" in m or "future rider" in m:
            return SX["puma_suede_xxi"]
        return SX["puma_suede_xxi"]
    if brand == "Reebok":
        if "club c" in m:
            return SX["reebok_classic_white"]
        if "classic leather" in m:
            return SX["reebok_classic_white"]
        if "freestyle" in m or "instapump" in m or "nano" in m or "bb 4000" in m:
            return SX["reebok_classic_white"]
        return SX["reebok_classic_white"]
    if brand == "Salomon":
        return SX["nb_2002r_rain"]
    if brand == "Asics":
        return SX["puma_suede_xxi"]
    if brand == "Vans":
        return SX["vans_old_skool"]
    if brand == "Converse":
        return SX["vans_old_skool"]
    if brand == "On Running":
        return SX["nb_990v6"]
    # rotation déterministe pour le reste (toujours une URL valide)
    pool = list(SX.values())
    h = int(hashlib.md5(model.encode("utf-8")).hexdigest(), 16)
    return pool[h % len(pool)]


def main() -> None:
    path = ROOT / "static" / "sneakers_db.json"
    db = json.loads(path.read_text(encoding="utf-8"))
    updated = 0
    for model, entry in db.items():
        if not isinstance(entry, dict):
            continue
        img = entry.get("image", "")
        brand = BRAND_BY_MODEL.get(model, "")
        entry.setdefault("ref", str(entry.get("ref") or ""))
        entry.setdefault("source_url", str(entry.get("source_url") or ""))
        entry.setdefault("source_title", str(entry.get("source_title") or ""))
        entry.setdefault("verified", bool(entry.get("verified", False)))
        entry.setdefault("brand", str(entry.get("brand") or brand).strip())

        if isinstance(img, str) and img.startswith("/static/images/") and "default-product" not in img:
            continue
        if isinstance(img, str) and ("secure-images.nike.com" in img or "images.stockx.com" in img):
            if "default-product" not in img:
                continue
        if isinstance(img, str) and "default-product.png" in img:
            entry["image"] = pick_stockx(model, brand)
            entry["brand"] = str(entry.get("brand") or brand).strip()
            entry["verified"] = False
            entry["source_url"] = ""
            entry["source_title"] = "apply_stockx_images (URL agrégée, non vérifiée SKU)"
            updated += 1
        elif not img or not isinstance(img, str):
            entry["image"] = pick_stockx(model, brand)
            entry["brand"] = str(entry.get("brand") or brand).strip()
            entry["verified"] = False
            entry["source_url"] = ""
            entry["source_title"] = "apply_stockx_images (URL agrégée, non vérifiée SKU)"
            updated += 1

    path.write_text(json.dumps(db, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("entries updated from default / normalized:", updated)


if __name__ == "__main__":
    main()
