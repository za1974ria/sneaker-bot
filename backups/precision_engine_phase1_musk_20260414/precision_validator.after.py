"""Precision Engine Phase 1: validation prix temps reel via Grok 4."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import requests

logger = logging.getLogger(__name__)


class GrokPrecisionValidator:
    """Validateur IA Grok 4 pour detection d'anomalies de prix sneakers."""

    def __init__(self) -> None:
        self.api_url = os.getenv("GROK_API_URL", "https://api.x.ai/v1/chat/completions")
        self.model = os.getenv("GROK_MODEL", "grok-4")
        self.timeout_sec = float(os.getenv("GROK_TIMEOUT_SEC", "25"))
        self.api_key = (os.getenv("GROK_API_KEY") or "").strip()

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        try:
            if value is None:
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _extract_json_object(text: str) -> dict[str, Any] | None:
        raw = (text or "").strip()
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            pass
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            parsed = json.loads(raw[start : end + 1])
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None

    def _fallback_result(self, *, explanation: str, suspicious: bool) -> dict[str, Any]:
        return {
            "confidence_score": 68 if suspicious else 82,
            "is_suspicious": suspicious,
            "risk_level": "HIGH" if suspicious else "MEDIUM",
            "explanation": explanation,
            "suggested_price": None,
            "reasoning": "Fallback local (appel Grok indisponible ou reponse invalide).",
        }

    def _build_prompt(
        self,
        *,
        title: str,
        current_price: float,
        avg_price: float,
        min_price: float,
        stock: str,
        site: str,
    ) -> str:
        # Prompt exact fourni (version Musk).
        return (
            "Tu es le meilleur analyste prix sneakers en France en 2026. Tu as une précision chirurgicale.\n\n"
            "Analyse ce produit :\n\n"
            f"Titre : {title}\n"
            f"Prix actuel : {current_price} €\n"
            f"Prix historique moyen (30j) : {avg_price} €\n"
            f"Prix le plus bas historique : {min_price} €\n"
            f"Stock actuel : {stock}\n"
            f"Site : {site}\n\n"
            "Réponds UNIQUEMENT en JSON valide :\n\n"
            "{\n"
            '  "confidence_score": integer 0-100,\n'
            '  "is_suspicious": boolean,\n'
            '  "risk_level": "LOW" | "MEDIUM" | "HIGH",\n'
            '  "explanation": "explication courte et précise en français (max 2 phrases)",\n'
            '  "suggested_price": integer ou null,\n'
            '  "reasoning": "raisonnement étape par étape"\n'
            "}\n\n"
            "Règles strictes :\n"
            "- Prix beaucoup plus bas que le min historique sans raison = HIGH suspicious\n"
            "- Baisse brutale > 35% en peu de temps = HIGH suspicious\n"
            "- Stock très élevé sur modèle rare = suspicious\n\n"
            "Sois extrêmement critique."
        )

    def validate(self, product_data: dict[str, Any]) -> dict[str, Any]:
        title = str(product_data.get("title") or "").strip()
        site = str(product_data.get("site") or "").strip() or "unknown_site"
        stock = str(product_data.get("stock") or "unknown")
        current_price = self._safe_float(product_data.get("current_price"))
        avg_price = self._safe_float(product_data.get("avg_price"))
        min_price = self._safe_float(product_data.get("min_price"))

        if not title or current_price is None:
            return self._fallback_result(
                explanation="Donnees insuffisantes (titre ou prix actuel manquant).",
                suspicious=True,
            )

        # Si historique absent, fallback intelligent base sur prix courant.
        avg_price = avg_price if avg_price is not None else current_price
        min_price = min_price if min_price is not None else current_price

        if not self.api_key:
            return self._fallback_result(
                explanation="GROK_API_KEY absent, validation IA ignoree.",
                suspicious=False,
            )

        prompt = self._build_prompt(
            title=title,
            current_price=current_price,
            avg_price=avg_price,
            min_price=min_price,
            stock=stock,
            site=site,
        )

        try:
            resp = requests.post(
                self.api_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "temperature": 0.0,
                    "messages": [
                        {"role": "system", "content": "Tu réponds uniquement avec un JSON valide."},
                        {"role": "user", "content": prompt},
                    ],
                },
                timeout=self.timeout_sec,
            )
            resp.raise_for_status()
            payload = resp.json()
            content = ((payload.get("choices") or [{}])[0].get("message") or {}).get("content")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Precision/Grok4] appel echoue title=%s err=%s", title, exc)
            return self._fallback_result(
                explanation=f"Validation Grok indisponible: {exc}",
                suspicious=False,
            )

        parsed = self._extract_json_object(str(content or ""))
        if not parsed:
            logger.warning("[Precision/Grok4] reponse non parseable title=%s content=%r", title, content)
            return self._fallback_result(
                explanation="Reponse Grok invalide/non parseable.",
                suspicious=True,
            )

        try:
            confidence_score = int(round(float(parsed.get("confidence_score"))))
        except (TypeError, ValueError):
            confidence_score = 70
        confidence_score = max(0, min(100, confidence_score))

        is_suspicious = bool(parsed.get("is_suspicious")) or confidence_score < 75
        risk_raw = str(parsed.get("risk_level") or "").upper().strip()
        risk_level = risk_raw if risk_raw in {"LOW", "MEDIUM", "HIGH"} else ("HIGH" if is_suspicious else "MEDIUM")
        explanation = str(parsed.get("explanation") or "").strip() or "Aucune explication."
        reasoning = str(parsed.get("reasoning") or "").strip() or "Aucun raisonnement detaille."

        suggested_price_raw = parsed.get("suggested_price")
        suggested_price: int | None
        if suggested_price_raw is None or str(suggested_price_raw).strip().lower() == "null":
            suggested_price = None
        else:
            try:
                suggested_price = int(round(float(suggested_price_raw)))
            except (TypeError, ValueError):
                suggested_price = None

        result = {
            "confidence_score": confidence_score,
            "is_suspicious": is_suspicious,
            "risk_level": risk_level,
            "explanation": explanation,
            "suggested_price": suggested_price,
            "reasoning": reasoning,
        }
        if is_suspicious:
            logger.warning(
                "[Precision/Grok4] suspicious title=%s site=%s price=%.2f avg=%.2f min=%.2f risk=%s conf=%s explain=%s",
                title,
                site,
                current_price,
                avg_price,
                min_price,
                risk_level,
                confidence_score,
                explanation,
            )
        return result


_VALIDATOR = GrokPrecisionValidator()


def validate_price_with_grok(product_data: dict[str, Any]) -> dict[str, Any]:
    """Facade fonctionnelle demandee (Phase 1)."""
    return _VALIDATOR.validate(product_data)

