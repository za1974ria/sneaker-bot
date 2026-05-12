"""Validation de precision prix via Grok 4 (xAI)."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import requests

logger = logging.getLogger(__name__)

_GROK_API_URL = os.getenv("GROK_API_URL", "https://api.x.ai/v1/chat/completions")
_GROK_MODEL = os.getenv("GROK_MODEL", "grok-4")
_GROK_TIMEOUT_SEC = float(os.getenv("GROK_TIMEOUT_SEC", "25"))


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Extrait le premier objet JSON valide d'une reponse texte."""
    txt = (text or "").strip()
    if not txt:
        return None
    try:
        parsed = json.loads(txt)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = txt.find("{")
    end = txt.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        parsed = json.loads(txt[start : end + 1])
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def validate_price_with_grok(product_data: dict[str, Any]) -> dict[str, Any]:
    """
    Valide un prix avec Grok 4 et renvoie:
      - confidence_score (0..100)
      - explanation
      - flag ("ok" ou "suspicious")
    """
    title = str(product_data.get("title") or "").strip()
    site = str(product_data.get("site") or "").strip()
    current_price = _safe_float(product_data.get("current_price"))
    history = product_data.get("price_history") or []
    if not isinstance(history, list):
        history = []
    hist_values = [h for h in (_safe_float(v) for v in history) if h is not None]

    base_result: dict[str, Any] = {
        "confidence_score": 100,
        "explanation": "Validation Grok non executee (fallback local).",
        "flag": "ok",
        "engine": "grok-4",
    }

    if not title or current_price is None:
        base_result["confidence_score"] = 65
        base_result["explanation"] = "Donnees insuffisantes pour valider (titre/prix manquants)."
        base_result["flag"] = "suspicious"
        logger.warning("[Precision/Grok] suspicious: donnees insuffisantes product_data=%s", product_data)
        return base_result

    api_key = (os.getenv("GROK_API_KEY") or "").strip()
    if not api_key:
        base_result["confidence_score"] = 80
        base_result["explanation"] = "GROK_API_KEY absent, validation IA ignoree."
        return base_result

    payload_context = {
        "title": title,
        "site": site,
        "current_price": current_price,
        "price_history": hist_values[-15:],
        "history_count": len(hist_values),
    }
    prompt = (
        "Tu es un validateur prix e-commerce ultra-strict.\n"
        "Objectif: detecter anomalies de prix (bug parsing, virgule/point, devise, outlier, typo).\n"
        "Analyse les donnees et renvoie UNIQUEMENT un JSON valide avec ce schema:\n"
        '{\n'
        '  "confidence_score": <entier 0..100>,\n'
        '  "explanation": "<phrase courte et concrete>",\n'
        '  "is_suspicious": <true|false>\n'
        "}\n"
        "Regles:\n"
        "- confidence_score eleve si prix coherent vs historique\n"
        "- confidence_score bas si ecart brutal non justifie\n"
        "- explanation en une phrase\n"
        "- aucune autre cle, aucun markdown.\n\n"
        f"Donnees produit:\n{json.dumps(payload_context, ensure_ascii=False)}"
    )

    try:
        response = requests.post(
            _GROK_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": _GROK_MODEL,
                "temperature": 0.0,
                "messages": [
                    {"role": "system", "content": "Reponds strictement en JSON."},
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=_GROK_TIMEOUT_SEC,
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Precision/Grok] appel echoue pour %s: %s", title, exc)
        base_result["confidence_score"] = 78
        base_result["explanation"] = f"Validation Grok indisponible: {exc}"
        return base_result

    content = (
        ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
        if isinstance(data, dict)
        else None
    )
    parsed = _extract_json_object(str(content or ""))
    if not parsed:
        logger.warning("[Precision/Grok] reponse non parseable pour %s: %r", title, content)
        base_result["confidence_score"] = 72
        base_result["explanation"] = "Reponse Grok non parseable."
        base_result["flag"] = "suspicious"
        return base_result

    score = parsed.get("confidence_score")
    try:
        confidence = int(round(float(score)))
    except (TypeError, ValueError):
        confidence = 70
    confidence = max(0, min(100, confidence))
    explanation = str(parsed.get("explanation") or "").strip() or "Aucune explication retournee."
    suspicious_by_ai = bool(parsed.get("is_suspicious"))
    is_suspicious = suspicious_by_ai or confidence < 75
    flag = "suspicious" if is_suspicious else "ok"

    result = {
        "confidence_score": confidence,
        "explanation": explanation,
        "flag": flag,
        "engine": "grok-4",
    }
    if is_suspicious:
        logger.warning(
            "[Precision/Grok] suspicious title=%s site=%s price=%.2f hist=%s score=%s explanation=%s",
            title,
            site,
            current_price,
            hist_values[-8:],
            confidence,
            explanation,
        )
    return result

