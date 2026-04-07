"""Signal engine réutilisable pour la décision market."""

from __future__ import annotations

import logging
from typing import TypedDict

logger = logging.getLogger(__name__)

VALID_TRENDS = {"UP", "DOWN", "STABLE"}
VALID_POSITIONS = {"BAS", "MOYEN", "HAUT"}


class SignalResult(TypedDict):
    label: str
    kind: str


def compute_signal(trend: str | None, position: str | None) -> SignalResult:
    """
    Retourne un signal métier stable et testable.

    Règles:
    - UP + BAS -> STRONG BUY
    - UP + MOYEN -> BUY
    - STABLE -> HOLD
    - DOWN + HAUT -> SELL
    - autre -> WAIT

    Garde-fous:
    - trend vide/invalid -> WAIT
    - position vide/inconnue -> WAIT (sauf pour STABLE qui reste HOLD)
    """
    raw_t = (trend or "").strip().upper()
    raw_p = (position or "").strip().upper()
    logger.debug("compute_signal input trend=%r position=%r", raw_t, raw_p)

    if not raw_t or raw_t not in VALID_TRENDS:
        logger.debug("compute_signal fallback WAIT (trend vide/invalide)")
        return {"label": "⚠️ WAIT", "kind": "wait"}

    if raw_t == "STABLE":
        logger.debug("compute_signal output HOLD")
        return {"label": "⚖️ HOLD", "kind": "hold"}

    if not raw_p or raw_p not in VALID_POSITIONS:
        logger.debug("compute_signal fallback WAIT (position vide/inconnue)")
        return {"label": "⚠️ WAIT", "kind": "wait"}

    if raw_t == "UP" and raw_p == "BAS":
        logger.debug("compute_signal output STRONG BUY")
        return {"label": "🔥 STRONG BUY", "kind": "buy-strong"}
    if raw_t == "UP" and raw_p == "MOYEN":
        logger.debug("compute_signal output BUY")
        return {"label": "🟢 BUY", "kind": "buy"}
    if raw_t == "DOWN" and raw_p == "HAUT":
        logger.debug("compute_signal output SELL")
        return {"label": "❌ SELL", "kind": "sell"}

    logger.debug("compute_signal output WAIT (cas par défaut)")
    return {"label": "⚠️ WAIT", "kind": "wait"}
