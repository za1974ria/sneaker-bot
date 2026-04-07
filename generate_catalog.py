#!/usr/bin/env python3
"""Génère catalog.csv (produit, min, max) à partir de catalog_master.csv."""

from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parent

if __name__ == "__main__":
    df = pd.read_csv(_ROOT / "catalog_master.csv", encoding="utf-8")

    df["min"] = 80
    df["max"] = 200

    df_out = df[["produit", "min", "max"]]
    df_out.to_csv(_ROOT / "catalog.csv", index=False, encoding="utf-8")

    print("✅ catalog.csv généré")
