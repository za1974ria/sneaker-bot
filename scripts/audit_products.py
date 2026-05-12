#!/usr/bin/env python3
"""Rapport rapide sur sneakers_db.json (verified, ref vs source_title)."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "static" / "sneakers_db.json"

with open(DB, encoding="utf-8") as f:
    data = json.load(f)

not_verified = []
ref_mismatch = []

for model, info in data.items():
    if not isinstance(info, dict):
        continue
    if not info.get("verified"):
        not_verified.append(model)

    ref = str(info.get("ref", "")).lower()
    title = str(info.get("source_title", "")).lower()

    if ref and ref not in title:
        ref_mismatch.append(model)

total = len(data)
verified_count = sum(1 for p in data.values() if isinstance(p, dict) and p.get("verified"))

print("=== AUDIT REPORT ===")
print(f"Total models: {total}")
print(f"Not verified: {len(not_verified)}")
print(not_verified[:20])

print(f"Ref mismatch: {len(ref_mismatch)}")
print(ref_mismatch[:20])

print(f"✅ VERIFIED: {verified_count}/{total}")
