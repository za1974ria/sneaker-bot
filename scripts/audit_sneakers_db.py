#!/usr/bin/env python3
"""
Audit sneakers_db.json : schéma brand/ref/image/source_*/verified, rapports, doublons.

  ./venv/bin/python scripts/audit_sneakers_db.py              # rapport
  ./venv/bin/python scripts/audit_sneakers_db.py --migrate    # ajoute champs manquants (sans deviner verified)
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "static" / "sneakers_db.json"
CAT_PATH = ROOT / "data" / "models_list.json"
REQUIRED = ("brand", "ref", "image", "source_url", "source_title", "verified")


def load_brand_by_model() -> dict[str, str]:
    if not CAT_PATH.is_file():
        return {}
    cat = json.loads(CAT_PATH.read_text(encoding="utf-8"))
    return {str(r["model"]): str(r["brand"]) for r in cat if r.get("model") and r.get("brand")}


def ref_in_text(ref: str, text: str) -> bool:
    if not ref or not text:
        return False
    r1 = ref.upper().replace("_", "-").strip()
    t = text.upper().replace("_", "-")
    r2 = re.sub(r"[^A-Z0-9]", "", ref.upper())
    t2 = re.sub(r"[^A-Z0-9]", "", text.upper())
    if len(r2) < 4:
        return False
    return r1 in t or r2 in t2


def suspicious_image(path: str) -> list[str]:
    reasons: list[str] = []
    if not path:
        reasons.append("empty_image")
        return reasons
    if path.startswith("http://") or path.startswith("https://"):
        reasons.append("remote_url_not_local")
    if "no-image" in path.lower():
        reasons.append("placeholder_no_image")
    if "default-product" in path.lower():
        reasons.append("generic_placeholder")
    return reasons


def migrate(db: dict[str, object], brand_by: dict[str, str]) -> int:
    n = 0
    for model, raw in list(db.items()):
        if not isinstance(raw, dict):
            continue
        before = json.dumps(raw, sort_keys=True)
        out = {
            "brand": str(raw.get("brand") or brand_by.get(model, "")).strip(),
            "ref": str(raw.get("ref") or "").strip(),
            "image": str(raw.get("image") or "").strip(),
            "source_url": str(raw.get("source_url") or "").strip(),
            "source_title": str(raw.get("source_title") or "").strip(),
            "verified": bool(raw.get("verified", False)),
        }
        db[model] = out
        if before != json.dumps(out, sort_keys=True):
            n += 1
    return n


def audit(db: dict[str, object], brand_by: dict[str, str]) -> dict[str, object]:
    total = 0
    verified_true = 0
    missing_schema = 0
    not_verified: list[str] = []
    title_missing_ref: list[str] = []
    dup_images: dict[str, list[str]] = defaultdict(list)
    suspicious: list[tuple[str, list[str]]] = []
    brand_mismatch: list[str] = []

    for model, raw in db.items():
        if not isinstance(raw, dict):
            continue
        total += 1
        if not all(k in raw for k in REQUIRED):
            missing_schema += 1
        ref = str(raw.get("ref") or "")
        img = str(raw.get("image") or "")
        st = str(raw.get("source_title") or "")
        su = str(raw.get("source_url") or "")
        v = bool(raw.get("verified", False))
        brand = str(raw.get("brand") or "")

        exp = brand_by.get(model, "")
        if exp and brand and brand != exp:
            brand_mismatch.append(model)

        if v:
            verified_true += 1
        else:
            not_verified.append(model)

        if st and ref and not str(st).upper().startswith("REJECT") and not ref_in_text(ref, st):
            title_missing_ref.append(model)

        if img:
            dup_images[img].append(model)
        sus = suspicious_image(img)
        if sus:
            suspicious.append((model, sus))

    dup_only = {k: v for k, v in dup_images.items() if len(v) > 1}

    to_review = len(
        set(not_verified)
        | set(title_missing_ref)
        | {m for m, _ in suspicious}
        | {m for vs in dup_only.values() for m in vs}
    )

    return {
        "total": total,
        "verified_true": verified_true,
        "missing_schema": missing_schema,
        "not_verified_count": len(not_verified),
        "title_missing_ref_count": len(title_missing_ref),
        "duplicate_image_paths": len(dup_only),
        "suspicious_count": len(suspicious),
        "brand_mismatch_count": len(brand_mismatch),
        "to_review_estimate": to_review,
        "lists": {
            "not_verified": not_verified,
            "title_missing_ref": title_missing_ref,
            "duplicate_images": dup_only,
            "suspicious": suspicious[:80],
            "brand_mismatch": brand_mismatch,
        },
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--migrate", action="store_true", help="Réécrit chaque fiche avec le schéma complet")
    args = p.parse_args()

    brand_by = load_brand_by_model()
    db = json.loads(DB_PATH.read_text(encoding="utf-8"))
    if not isinstance(db, dict):
        print("JSON racine invalide")
        return 1

    if args.migrate:
        n = migrate(db, brand_by)
        DB_PATH.write_text(json.dumps(db, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Migration: {n} fiches normalisées → {DB_PATH}")

    rep = audit(db, brand_by)
    lists = rep.pop("lists")
    print("\n=== Rapport sneakers_db ===")
    for k, v in rep.items():
        if k != "lists":
            print(f"  {k}: {v}")
    print(f"  rejetés (non vérifiés / preuve absente): {rep['not_verified_count']}")
    print("\n--- Détail (extraits) ---")
    print("non verified (max 15):", ", ".join(lists["not_verified"][:15]) + (" …" if len(lists["not_verified"]) > 15 else ""))
    print("source_title sans ref (max 15):", ", ".join(lists["title_missing_ref"][:15]) + (" …" if len(lists["title_missing_ref"]) > 15 else ""))
    if lists["duplicate_images"]:
        print("\nDoublons image (chemin → modèles):")
        for path, models in list(lists["duplicate_images"].items())[:12]:
            print(f"  {path}: {', '.join(models)}")
    if lists["suspicious"]:
        print("\nImages suspectes (max 10):")
        for m, rs in lists["suspicious"][:10]:
            print(f"  {m}: {', '.join(rs)}")
    if lists["brand_mismatch"]:
        print("\nbrand ≠ catalogue:", ", ".join(lists["brand_mismatch"][:20]))
    print("\n=== Fin ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
