#!/usr/bin/env python3
"""Orchestrateur de mise à jour marché avec logging fichier."""

from __future__ import annotations

import logging
from pathlib import Path

from run_scraper import run_market_update

ROOT = Path(__file__).resolve().parent
LOG_PATH = ROOT / "market_update.log"


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.FileHandler(LOG_PATH, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


def main() -> int:
    _setup_logging()
    logging.info("Démarrage update market")
    try:
        code = run_market_update()
        if code == 0:
            logging.info("Mise à jour terminée avec succès")
        else:
            logging.warning("Mise à jour terminée sans nouvelles données")
        return code
    except Exception as e:  # noqa: BLE001
        logging.exception("Échec update market: %s", e)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
