"""
Pré-chargement des CSV marché FR en mémoire pour réduire les I/O disque répétées.
Rechargé après chaque refresh FR (via fr_job_runner).
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_PROJECT_DATA = Path(__file__).resolve().parent.parent / "data"
MARKET_FR_PATH = _PROJECT_DATA / "market_fr.csv"
MARKET_FR_SOURCES_PATH = _PROJECT_DATA / "market_fr_sources.csv"

_preload: dict[str, str] = {"market_fr": "", "market_fr_sources": ""}


def preload_market_csvs() -> None:
    """Lit les CSV au démarrage (best-effort)."""
    _reload_unlocked()


def reload_after_market_update() -> None:
    """Appelé après agrégation / refresh pour resynchroniser la mémoire."""
    _reload_unlocked()


def _reload_unlocked() -> None:
    for key, path in (
        ("market_fr", MARKET_FR_PATH),
        ("market_fr_sources", MARKET_FR_SOURCES_PATH),
    ):
        try:
            if path.is_file():
                _preload[key] = path.read_text(encoding="utf-8", errors="replace")
            else:
                _preload[key] = ""
        except Exception as exc:  # noqa: BLE001
            logger.debug("csv_preload %s: %s", key, exc)
            _preload[key] = ""


def get_csv_text(name: str) -> str | None:
    """Retourne le contenu préchargé ou None si vide."""
    raw = _preload.get(name) or ""
    if raw.strip():
        return raw
    return None
