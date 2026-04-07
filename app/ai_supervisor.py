"""AI supervisor for scraped sneaker market prices (local rules + optional Groq)."""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import re
import statistics
from collections import Counter
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _load_project_dotenv() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    except Exception:
        pass


_load_project_dotenv()

DEFAULT_ANALYSIS = {
    "credible": False,
    "confidence": 0,
    "anomalies": ["analyse indisponible"],
    "verdict": "Impossible d'evaluer ce modele",
    "action": "WARN",
    "decision_source": "local_ruleset_v2",
    "control_mode": "strict",
}

PRICE_RANGES = {
    "Nike": (55, 350),
    "Adidas": (45, 280),
    "New Balance": (55, 350),
    "Salomon": (90, 320),
    "Asics": (55, 280),
    "Puma": (40, 220),
    "Reebok": (40, 220),
    "Vans": (45, 200),
    "Converse": (40, 200),
    "On Running": (90, 350),
    "default": (30, 400),
}

GROQ_MODEL_DEFAULT = "llama-3.1-8b-instant"
_CACHE_LOCK = threading.Lock()
_GROQ_CLIENT = None
_GROQ_CLIENT_KEY: str | None = None


def _get_groq_client(api_key: str) -> Any:
    global _GROQ_CLIENT, _GROQ_CLIENT_KEY
    if not api_key:
        return None
    if _GROQ_CLIENT is not None and _GROQ_CLIENT_KEY == api_key:
        return _GROQ_CLIENT
    try:
        from groq import Groq

        _GROQ_CLIENT = Groq(api_key=api_key)
        _GROQ_CLIENT_KEY = api_key
        return _GROQ_CLIENT
    except Exception as exc:  # noqa: BLE001
        logger.warning("Initialisation client Groq impossible: %s", exc)
        return None


class AISupervisor:
    def __init__(self) -> None:
        root_dir = Path(__file__).resolve().parent.parent
        self.data_dir = root_dir / "data"
        self.logs_dir = root_dir / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.supervisor_logger = logging.getLogger("ai_supervisor")
        self.supervisor_logger.setLevel(logging.INFO)
        if not self.supervisor_logger.handlers:
            handler = logging.FileHandler(self.logs_dir / "supervisor.log", encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            self.supervisor_logger.addHandler(handler)
        self.control_mode = (os.getenv("AI_CONTROL_MODE") or "strict").strip().lower()
        self.allow_remote_ai = (os.getenv("ALLOW_REMOTE_AI") or "0").strip() == "1"
        grok_key = (os.getenv("GROK_API_KEY") or "").strip()
        groq_key = (os.getenv("GROQ_API_KEY") or "").strip()
        gemini_key = (os.getenv("GEMINI_API_KEY") or "").strip()
        self.grok_key_configured = bool(grok_key or groq_key)
        self.gemini_key_configured = bool(gemini_key)
        self._groq_api_key = groq_key
        self._groq_disabled = (os.getenv("DISABLE_GROQ_VALIDATION") or "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        self._groq_model = (os.getenv("GROQ_MODEL") or GROQ_MODEL_DEFAULT).strip()
        self._cache_path = self.data_dir / "groq_price_validation_cache.json"
        self._groq_client: Any = None
        if self._groq_api_key and not self._groq_disabled:
            self._groq_client = _get_groq_client(self._groq_api_key)

    def _cache_path_read(self) -> dict[str, Any]:
        if not self._cache_path.is_file():
            return {"entries": {}}
        try:
            raw = json.loads(self._cache_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return {"entries": {}}
            raw.setdefault("entries", {})
            if not isinstance(raw["entries"], dict):
                raw["entries"] = {}
            return raw
        except Exception as exc:  # noqa: BLE001
            logger.warning("Lecture cache Groq impossible: %s", exc)
            return {"entries": {}}

    def _cache_path_write(self, data: dict[str, Any]) -> None:
        cache_path = Path(self._cache_path)
        tmp_path = cache_path.with_suffix(".tmp")
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            with tmp_path.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            tmp_path.replace(cache_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Ecriture cache Groq impossible: %s", exc)

    @staticmethod
    def _groq_prices_fingerprint(prices: list[float]) -> str:
        norm = sorted(round(float(p), 0) for p in prices)
        return hashlib.md5(str(norm).encode()).hexdigest()[:8]

    def _cache_get(self, cache_key: str) -> dict[str, Any] | None:
        with _CACHE_LOCK:
            data = self._cache_path_read()
            ent = data["entries"].get(cache_key)
            if not isinstance(ent, dict):
                return None
            res = ent.get("result")
            return res if isinstance(res, dict) else None

    def _cache_set(self, cache_key: str, result: dict[str, Any]) -> None:
        with _CACHE_LOCK:
            data = self._cache_path_read()
            data["entries"][cache_key] = {"result": result}
            if len(data["entries"]) > 2000:
                for k in list(data["entries"].keys())[:500]:
                    data["entries"].pop(k, None)
            self._cache_path_write(data)

    @staticmethod
    def _strip_json_blob(raw: str) -> str:
        s = raw.strip()
        if s.startswith("```"):
            s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
            s = re.sub(r"\s*```$", "", s)
        return s.strip()

    def _normalize_validate_result(self, raw: dict[str, Any], prices: list[float]) -> dict[str, Any]:
        valid = bool(raw.get("valid", False))
        reason = str(raw.get("reason") or "")
        sr = raw.get("suggested_range")
        if not (isinstance(sr, list) and len(sr) >= 2):
            sr = [min(prices), max(prices)] if prices else [0.0, 0.0]
        try:
            lo, hi = float(sr[0]), float(sr[1])
        except (TypeError, ValueError, IndexError):
            lo, hi = (min(prices), max(prices)) if prices else (0.0, 0.0)

        conf = raw.get("confidence", 0.5)
        try:
            c = float(conf)
            if c > 1.0:
                c = c / 100.0
            c = max(0.0, min(1.0, c))
        except (TypeError, ValueError):
            c = 0.5

        anom_raw = raw.get("anomalies") or []
        anom_list: list[float] = []
        if isinstance(anom_raw, list):
            for x in anom_raw:
                try:
                    anom_list.append(float(x))
                except (TypeError, ValueError):
                    pass

        return {
            "valid": valid,
            "reason": reason,
            "suggested_range": [lo, hi],
            "confidence": c,
            "anomalies": anom_list,
        }

    def _local_validate_prices_result(self, model_name: str, brand: str, prices: list[float]) -> dict[str, Any]:
        if not prices:
            return {
                "valid": False,
                "reason": "Aucun prix disponible",
                "suggested_range": [0.0, 0.0],
                "confidence": 0.0,
                "anomalies": [],
            }
        floor, ceiling = PRICE_RANGES.get(brand, PRICE_RANGES["default"])
        anomalies = [p for p in prices if p < max(30.0, floor * 0.35) or p > min(2000.0, ceiling * 2.5)]
        valid_prices = [p for p in prices if p not in anomalies]
        if not valid_prices:
            valid_prices = [p for p in prices if 30.0 <= p <= 2000.0]
        if not valid_prices:
            return {
                "valid": False,
                "reason": "Validation locale (Groq indisponible) — aucun prix dans les bornes",
                "suggested_range": [0.0, 0.0],
                "confidence": 0.35,
                "anomalies": anomalies,
            }
        return {
            "valid": True,
            "reason": "Validation locale (Groq indisponible)",
            "suggested_range": [min(valid_prices), max(valid_prices)],
            "confidence": 0.5,
            "anomalies": anomalies,
        }

    def _groq_validate_raw(self, model_name: str, brand: str, prices: list[float]) -> dict[str, Any]:
        time.sleep(0.5)
        prompt = self._build_groq_prompt(model_name, brand, prices)
        assert self._groq_client is not None
        response = self._groq_client.chat.completions.create(
            model=self._groq_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Expert prix sneakers marché français. "
                        "Tu réponds UNIQUEMENT par un objet JSON valide, sans markdown."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=280,
        )
        raw_text = (response.choices[0].message.content or "").strip()
        raw_text = self._strip_json_blob(raw_text)
        return json.loads(raw_text)

    def _build_groq_prompt(self, model_name: str, brand: str, prices: list[float]) -> str:
        from app.brand_price_rules import get_price_range

        expected_min, expected_max = get_price_range(brand, model_name)
        avg = sum(prices) / len(prices)
        med = float(statistics.median(prices))
        min_p = min(prices)
        max_p = max(prices)
        sorted_prices = sorted(prices)
        price_counts = Counter(round(p, 0) for p in prices)
        doublons = [p for p, count in price_counts.items() if count > 2]

        return f"""Validation marché FR — {brand} {model_name}

Prix ({len(prices)} sources) : {sorted_prices}
Statistiques : min={min_p:.2f}\u00a0€ max={max_p:.2f}\u00a0€ moy={avg:.2f}\u00a0€ médiane={med:.2f}\u00a0€
Référence marché (indicatif) : {expected_min}\u00a0€–{expected_max}\u00a0€ pour {brand}
Doublons fréquents : {doublons if doublons else "aucun"}

RÈGLES ABSOLUES (à respecter strictement) :
1) Si TOUS les prix sont entre 50\u00a0€ et 300\u00a0€ → "valid": true.
2) Si la moyenne est cohérente avec une sneaker {brand} (même hors fourchette stricte) et les prix sont réalistes → "valid": true.
3) "valid": false UNIQUEMENT si au moins une de ces conditions :
   - un prix < 20\u00a0€ ou > 500\u00a0€
   - OU écart min/max > 400% (autrement dit max > 5× min avec min > 0)
4) Ne jamais mettre "valid": false si la moyenne est entre 80\u00a0€ et 200\u00a0€ (sneakers courantes).
5) confidence : 0.85 par défaut si les prix semblent normaux ; baisse seulement si tu vois un vrai problème (ex. prix < 20\u00a0€ ou > 500\u00a0€ ou spread > 400%).
6) "anomalies" : liste des prix numériques aberrants uniquement (sinon []).

Ne pas être strict sur de petits écarts entre boutiques. Promotions et coloris = variations normales.

Réponds UNIQUEMENT par un JSON (sans markdown) :
{{
  "valid": true,
  "reason": "court",
  "suggested_range": [{expected_min}, {expected_max}],
  "confidence": 0.85,
  "anomalies": []
}}"""

    @staticmethod
    def _groq_apply_safety_rules(prices: list[float], result: dict[str, Any]) -> dict[str, Any]:
        """
        Réduit les faux positifs Groq : aligné sur le prompt (rejet seulement si <20€, >500€ ou spread >400%).
        """
        if not prices:
            return result
        out = dict(result)
        lo = float(min(prices))
        hi = float(max(prices))
        avg = sum(float(p) for p in prices) / len(prices)
        spread_ratio = (hi / lo) if lo > 0 else 999.0
        catastrophic = lo < 20.0 or hi > 500.0 or (lo > 0 and spread_ratio > 5.0)

        try:
            c = float(out.get("confidence") or 0.0)
            if c > 1.0:
                c /= 100.0
            c = max(0.0, min(1.0, c))
        except (TypeError, ValueError):
            c = 0.5

        # Jamais valid=False pour une moyenne « sneaker courante » 80–200€
        if 80.0 <= avg <= 200.0:
            out["valid"] = True
            out["confidence"] = max(0.85, c)
            return out

        if all(50.0 <= float(p) <= 300.0 for p in prices):
            out["valid"] = True
            out["confidence"] = max(0.85, c)
            return out

        if not catastrophic:
            out["valid"] = True
            out["confidence"] = max(0.85, c)
        return out

    def validate_prices(self, model_name: str, brand: str, prices: list[float]) -> dict[str, Any]:
        """
        Valide une liste de prix bruts (scraping). Utilise Groq si clé configurée, sinon règles locales.
        Cache par clé brand|model|hash(prix triés) pour limiter les appels API.
        Retour : valid, reason, suggested_range [min,max], confidence (0..1), anomalies (liste de floats).
        """
        prices_f = [float(p) for p in prices if p is not None]
        if not prices_f:
            return {
                "valid": False,
                "reason": "Aucun prix disponible",
                "suggested_range": [0.0, 0.0],
                "confidence": 0.0,
                "anomalies": [],
            }

        fp = self._groq_prices_fingerprint(prices_f)
        cache_key = f"{brand.strip()}|{model_name.strip()}|{fp}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            logger.debug("Groq cache hit %s", cache_key)
            return dict(self._groq_apply_safety_rules(prices_f, dict(cached)))

        if not self._groq_client:
            logger.warning("GROQ_API_KEY manquante ou Groq désactivé — fallback local (%s)", cache_key)
            result = self._local_validate_prices_result(model_name, brand, prices_f)
            self._cache_set(cache_key, result)
            return result

        try:
            raw = self._groq_validate_raw(model_name, brand, prices_f)
            if not isinstance(raw, dict):
                raise ValueError("Réponse Groq non-objet JSON")
            result = self._normalize_validate_result(raw, prices_f)
            result = self._groq_apply_safety_rules(prices_f, result)
            result["decision_source"] = "groq_llama_3_1_8b_instant"
            logger.info(
                "Groq validation %s | valid=%s confidence=%.2f",
                cache_key,
                result.get("valid"),
                float(result.get("confidence") or 0),
            )
        except json.JSONDecodeError as exc:
            logger.error("Groq JSON invalide pour %s: %s", cache_key, exc)
            result = self._local_validate_prices_result(model_name, brand, prices_f)
            result["decision_source"] = "local_fallback_json_error"
        except Exception as exc:  # noqa: BLE001
            logger.error("Groq erreur pour %s: %s", cache_key, exc)
            result = self._local_validate_prices_result(model_name, brand, prices_f)
            result["decision_source"] = "local_fallback_error"

        if "decision_source" not in result:
            result["decision_source"] = "local_ruleset_v2"
        self._cache_set(cache_key, result)
        return result

    def _is_effective_ok(self, action: str, anomalies: Any) -> bool:
        action_norm = (action or "").upper()
        anomalies_list = anomalies if isinstance(anomalies, list) else []
        return action_norm == "OK" or (action_norm == "WARN" and len(anomalies_list) == 0)

    def analyze_prices(
        self,
        brand: str,
        model: str,
        market: str,
        price_min: float,
        price_max: float,
        price_avg: float,
    ) -> dict[str, Any]:
        try:
            price_min = float(price_min)
            price_max = float(price_max)
            price_avg = float(price_avg)

            floor, ceiling = PRICE_RANGES.get(brand, PRICE_RANGES["default"])
            anomalies: list[str] = []

            if price_min < floor * 0.3:
                anomalies.append(f"Prix minimum suspect: {price_min}\u00a0€ (plancher: {floor}\u00a0€)")

            if price_max > ceiling * 2.0:
                anomalies.append(f"Prix maximum suspect: {price_max}\u00a0€ (plafond: {ceiling}\u00a0€)")

            if price_min > 0 and (price_max / price_min) > 2.2:
                anomalies.append(f"Écart min/max suspect: {price_min}\u00a0€ → {price_max}\u00a0€")
            if not (price_min <= price_avg <= price_max):
                anomalies.append(f"Moyenne hors plage: avg={price_avg}\u00a0€")

            if anomalies:
                action = "ALERT" if len(anomalies) >= 2 else "WARN"
                credible = False
                confidence = 30 if action == "ALERT" else 40
            else:
                action = "OK"
                credible = True
                confidence = 95

            return {
                "credible": credible,
                "confidence": confidence,
                "anomalies": anomalies,
                "verdict": "Prix cohérents" if not anomalies else "Anomalie détectée",
                "action": action,
                "decision_source": "local_ruleset_v2",
                "control_mode": self.control_mode,
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("Local analyze_prices failed for %s %s %s: %s", brand, model, market, exc)
            fallback = dict(DEFAULT_ANALYSIS)
            fallback["anomalies"] = [f"erreur analyse locale: {exc}"]
            fallback["verdict"] = "Analyse indisponible (erreur locale)"
            return fallback

    def control_status(self) -> dict[str, Any]:
        try:
            import openai as _openai  # type: ignore

            openai_sdk_available = True
            openai_sdk_version = str(getattr(_openai, "__version__", "unknown"))
        except Exception:
            openai_sdk_available = False
            openai_sdk_version = "not_installed"
        groq_pkg = False
        try:
            import groq as _groq  # noqa: F401

            groq_pkg = True
        except Exception:
            pass
        return {
            "control_mode": self.control_mode,
            "decision_source": "local_ruleset_v2",
            "allow_remote_ai": self.allow_remote_ai,
            "grok_key_configured": self.grok_key_configured,
            "gemini_key_configured": self.gemini_key_configured,
            "openai_sdk_available": openai_sdk_available,
            "openai_sdk_version": openai_sdk_version,
            "groq_package_available": groq_pkg,
            "groq_client_ready": self._groq_client is not None,
            "groq_model": self._groq_model,
            "groq_validation_disabled": self._groq_disabled,
        }

    def supervise_market(self, market: str) -> list[dict[str, Any]]:
        market_norm = "FR"
        csv_path = self.data_dir / f"market_{market_norm.lower()}.csv"
        if not csv_path.is_file():
            return []
        results: list[dict[str, Any]] = []
        with csv_path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                brand = str(row.get("brand") or "").strip()
                model = str(row.get("model") or "").strip()
                if not brand or not model:
                    continue
                try:
                    price_min = float(row.get("price_min") or 0.0)
                    price_max = float(row.get("price_max") or 0.0)
                    price_avg = float(row.get("price_avg") or 0.0)
                except (TypeError, ValueError):
                    continue
                analysis = self.analyze_prices(
                    brand=brand,
                    model=model,
                    market=market_norm,
                    price_min=price_min,
                    price_max=price_max,
                    price_avg=price_avg,
                )
                record = {
                    "brand": brand,
                    "model": model,
                    "market": market_norm,
                    "price_min": price_min,
                    "price_max": price_max,
                    "price_avg": price_avg,
                    **analysis,
                }
                action = str(record.get("action") or "WARN").upper()
                if action in {"WARN", "ALERT"}:
                    self.supervisor_logger.warning(
                        "[%s] %s %s -> %s | anomalies=%s",
                        market_norm,
                        brand,
                        model,
                        action,
                        record.get("anomalies"),
                    )
                results.append(record)
        return results

    def get_health_report(self) -> dict[str, Any]:
        all_results: list[dict[str, Any]] = []
        all_results.extend(self.supervise_market("FR"))
        ok = sum(
            1
            for r in all_results
            if self._is_effective_ok(str(r.get("action") or ""), r.get("anomalies"))
        )
        warns = sum(
            1
            for r in all_results
            if str(r.get("action") or "").upper() == "WARN"
            and not self._is_effective_ok(str(r.get("action") or ""), r.get("anomalies"))
        )
        alerts = sum(1 for r in all_results if str(r.get("action") or "").upper() == "ALERT")
        total = len(all_results)
        score = round((ok / total) * 100, 2) if total > 0 else 0.0
        anomalies = [
            {
                "brand": r.get("brand"),
                "model": r.get("model"),
                "market": r.get("market"),
                "action": r.get("action"),
                "anomalies": r.get("anomalies"),
                "verdict": r.get("verdict"),
                "confidence": r.get("confidence"),
            }
            for r in all_results
            if str(r.get("action") or "").upper() == "ALERT"
            or (
                str(r.get("action") or "").upper() == "WARN"
                and not self._is_effective_ok(str(r.get("action") or ""), r.get("anomalies"))
            )
        ]
        return {
            "total_models": total,
            "ok": ok,
            "warnings": warns,
            "alerts": alerts,
            "score": score,
            "anomalies": anomalies,
            "results": all_results,
        }
