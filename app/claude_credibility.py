"""
Second avis LLM (Anthropic Claude) pour renforcer le score de crédibilité des prix,
en complément de la validation chiffrée Groq (ai_supervisor).

- Bonus optionnel 0–5 points (plafonné à 100 dans confidence_scorer).
- Inactif si ANTHROPIC_API_KEY absente ou DISABLE_CLAUDE_CREDIBILITY=1.
- Cache disque (TTL 7 j par défaut) + repli local (_local_credibility) si l’API échoue.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CACHE_LOCK = threading.Lock()
_CLIENT_LOCK = threading.Lock()
_ANTHROPIC_CLIENT: Any = None
_ANTHROPIC_CLIENT_KEY: str | None = None
_DEFAULT_MODEL = "claude-haiku-4-5-20251001"


def _cache_ttl_seconds() -> float:
    """Durée de validité des entrées cache (défaut 168 h = 7 j)."""
    try:
        h = float((os.getenv("CLAUDE_CREDIBILITY_CACHE_TTL_HOURS") or "168").strip() or "168")
    except ValueError:
        h = 168.0
    return max(0.0, h * 3600.0)


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    except Exception:
        pass


_load_dotenv()


def claude_credibility_enabled() -> bool:
    if (os.getenv("DISABLE_CLAUDE_CREDIBILITY") or "").strip().lower() in ("1", "true", "yes"):
        return False
    return bool((os.getenv("ANTHROPIC_API_KEY") or "").strip())


def init_claude_cache() -> None:
    """Crée le dossier data et un fichier cache JSON vide si besoin."""
    data_dir = Path(__file__).resolve().parent.parent / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    cache_file = data_dir / "claude_credibility_cache.json"
    if not cache_file.is_file():
        try:
            cache_file.write_text('{"entries": {}}\n', encoding="utf-8")
        except OSError as exc:  # noqa: BLE001
            logger.warning("init_claude_cache: création fichier impossible: %s", exc)


def _cache_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "claude_credibility_cache.json"


def _cache_read_unlocked() -> dict[str, Any]:
    p = _cache_path()
    if not p.is_file():
        return {"entries": {}}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {"entries": {}}
        raw.setdefault("entries", {})
        if not isinstance(raw["entries"], dict):
            raw["entries"] = {}
        return raw
    except Exception as exc:  # noqa: BLE001
        logger.warning("Lecture cache Claude crédibilité impossible: %s", exc)
        return {"entries": {}}


def _cache_write_unlocked(data: dict[str, Any]) -> None:
    p = _cache_path()
    tmp = p.with_suffix(".tmp")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(p)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Écriture cache Claude crédibilité impossible: %s", exc)


def _make_cache_key(
    brand: str,
    model: str,
    price_min: float,
    price_max: float,
    price_avg: float,
    nb_sources: int,
    groq_valid: bool | None,
    groq_confidence: float | None,
) -> str:
    gv = "x" if groq_valid is None else ("1" if groq_valid else "0")
    gc = round(float(groq_confidence or 0.0), 2)
    payload = (
        f"{brand.strip()}|{model.strip()}|{price_min:.1f}|{price_max:.1f}|{price_avg:.1f}|"
        f"{nb_sources}|{gv}|{gc}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _get_cache(cache_key: str) -> int | None:
    """Retourne le bonus 0–5 si présent en cache et non expiré, sinon None."""
    with _CACHE_LOCK:
        data = _cache_read_unlocked()
        ent = data["entries"].get(cache_key)
        if not isinstance(ent, dict) or "bonus" not in ent:
            return None
        ttl = _cache_ttl_seconds()
        if ttl > 0:
            try:
                ts = float(ent.get("ts") or 0.0)
            except (TypeError, ValueError):
                ts = 0.0
            if ts <= 0.0 or (time.time() - ts) > ttl:
                return None
        try:
            return max(0, min(5, int(ent["bonus"])))
        except (TypeError, ValueError):
            return None


def _set_cache(cache_key: str, bonus: int) -> None:
    """Enregistre le bonus et limite la taille du cache."""
    b = max(0, min(5, int(bonus)))
    with _CACHE_LOCK:
        data = _cache_read_unlocked()
        data["entries"][cache_key] = {"bonus": b, "ts": time.time()}
        if len(data["entries"]) > 1500:
            for k in list(data["entries"].keys())[:400]:
                data["entries"].pop(k, None)
        _cache_write_unlocked(data)


def _local_credibility(
    *,
    brand: str,
    model: str,
    price_min: float,
    price_max: float,
    price_avg: float,
    nb_sources: int,
    groq_valid: bool | None,
    groq_confidence: float | None,
) -> int:
    """
    Heuristique locale 0–5 sans appel API (repli si Claude indisponible ou erreur).
    """
    _ = (brand, model)  # réservé pour extensions (gammes par marque)
    score = 2
    if nb_sources >= 3:
        score += 1
    elif nb_sources < 2:
        score -= 1

    spread_pct = (price_max - price_min) / price_avg * 100.0 if price_avg > 0 else 999.0
    if spread_pct <= 35:
        score += 1
    elif spread_pct > 80:
        score -= 1

    if groq_valid is True:
        score += 1
    elif groq_valid is False:
        score = min(score, 2)

    if groq_confidence is not None:
        gc = float(groq_confidence)
        if gc >= 0.75:
            score += 1
        elif gc < 0.35:
            score -= 1

    return max(0, min(5, score))


def _strip_json_blob(raw: str) -> str:
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


def _get_anthropic_client(api_key: str) -> Any:
    global _ANTHROPIC_CLIENT, _ANTHROPIC_CLIENT_KEY
    if not api_key:
        return None
    with _CLIENT_LOCK:
        if _ANTHROPIC_CLIENT is not None and _ANTHROPIC_CLIENT_KEY == api_key:
            return _ANTHROPIC_CLIENT
        try:
            import anthropic

            _ANTHROPIC_CLIENT = anthropic.Anthropic(api_key=api_key)
            _ANTHROPIC_CLIENT_KEY = api_key
            return _ANTHROPIC_CLIENT
        except ImportError as exc:  # noqa: BLE001
            raise RuntimeError("Paquet anthropic requis : pip install anthropic") from exc


def _call_claude_messages(api_key: str, model: str, system: str, user_text: str) -> str:
    client = _get_anthropic_client(api_key)
    if client is None:
        raise RuntimeError("Client Anthropic indisponible")
    msg = client.messages.create(
        model=model,
        max_tokens=320,
        system=system,
        messages=[{"role": "user", "content": user_text}],
        temperature=0.2,
    )
    parts: list[str] = []
    for block in msg.content:
        btype = getattr(block, "type", None) or (isinstance(block, dict) and block.get("type"))
        if btype == "text":
            txt = getattr(block, "text", None)
            if txt is None and isinstance(block, dict):
                txt = block.get("text")
            parts.append(str(txt or ""))
    return "".join(parts).strip()


def _credibility_result_dict(
    *,
    enabled: bool,
    claude_bonus: int | None,
    groq_valid: bool | None,
    groq_confidence: float | None,
    source: str,
) -> dict[str, Any]:
    """Construit le dict retourné par analyze_credibility."""
    if not enabled:
        return {
            "enabled": False,
            "claude_bonus": None,
            "combined_score": 0,
            "reliability": "inconnue",
            "source": source,
        }
    bonus = max(0, min(5, int(claude_bonus or 0)))
    gc = max(0.0, min(1.0, float(groq_confidence or 0.0)))
    combined = int(28 + bonus * 14 + gc * 38)
    if groq_valid is True:
        combined += 12
    elif groq_valid is False:
        combined = min(combined, 52)
    combined = max(0, min(100, combined))

    if combined >= 72:
        rel = "haute"
    elif combined >= 48:
        rel = "moyenne"
    else:
        rel = "basse"

    market_coherence = "faible"
    if combined >= 72:
        market_coherence = "élevée"
    elif combined >= 48:
        market_coherence = "moyenne"

    return {
        "enabled": True,
        "claude_bonus": bonus,
        "combined_score": combined,
        "reliability": rel,
        "market_coherence": market_coherence,
        "source": source,
    }


def analyze_credibility(
    brand: str,
    model: str,
    price_min: float,
    price_max: float,
    price_avg: float,
    nb_sources: int = 1,
    groq_valid: bool | None = None,
    groq_confidence: float | None = None,
    *,
    updated_at: str = "",
    groq_reason: str = "",
) -> dict[str, Any]:
    """
    Analyse crédibilité (Claude + cache + repli local), complément Groq.

    Retourne un dict avec notamment :
    - enabled, claude_bonus (0–5), combined_score (0–100), reliability, source
    """
    init_claude_cache()

    if not claude_credibility_enabled():
        return _credibility_result_dict(
            enabled=False,
            claude_bonus=None,
            groq_valid=groq_valid,
            groq_confidence=groq_confidence,
            source="disabled",
        )

    cache_key = _make_cache_key(
        brand,
        model,
        price_min,
        price_max,
        price_avg,
        nb_sources,
        groq_valid,
        groq_confidence,
    )
    cached = _get_cache(cache_key)
    if cached is not None:
        return _credibility_result_dict(
            enabled=True,
            claude_bonus=cached,
            groq_valid=groq_valid,
            groq_confidence=groq_confidence,
            source="cache",
        )

    api_key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
    claude_model = (os.getenv("CLAUDE_MODEL") or _DEFAULT_MODEL).strip()

    spread_pct = 0.0
    if price_avg > 0:
        spread_pct = (price_max - price_min) / price_avg * 100.0

    gv_txt = (
        "inconnu"
        if groq_valid is None
        else ("oui (prix jugés plausibles)" if groq_valid else "non (Groq signale des anomalies)")
    )
    gc_txt = f"{float(groq_confidence or 0.0):.2f}" if groq_confidence is not None else "N/A"
    reason_txt = (groq_reason or "").strip()[:400]

    system = (
        "Tu es un expert du marché sneakers France (prix e-commerce neuf / retailers). "
        "Tu donnes un avis court en JSON uniquement, sans markdown."
    )
    user_prompt = f"""Contexte agrégé (marché FR) :
- Marque : {brand}
- Modèle : {model}
- Prix min / moy / max : {price_min:.2f}\u00a0€ / {price_avg:.2f}\u00a0€ / {price_max:.2f}\u00a0€
- Écart min-max en % de la moyenne : {spread_pct:.1f}%
- Nombre de sources agrégées : {nb_sources}
- Dernière mise à jour (si fournie) : {updated_at or "N/A"}
- Validation Groq (autre LLM, chiffres) : plausible={gv_txt}, confidence 0-1={gc_txt}
- Motif Groq (extrait) : {reason_txt or "N/A"}

Tâche : attribue un bonus de crédibilité entier **bonus_points** entre 0 et 5 pour ce tableau de prix
(qualité globale de la cohérence marché, plausibilité vs segment marque/modèle, alignement ou nuance vs l'avis Groq).
- 0 = doute fort ou données trop pauvres / incohérentes
- 2-3 = plausible standard
- 4-5 = très cohérent et crédible pour ce modèle

Réponds UNIQUEMENT un JSON sur une ligne ou plusieurs, sans texte autour :
{{"bonus_points": <int 0-5>, "note": "<une phrase courte en français>"}}"""

    bonus: int
    src = "claude"
    try:
        raw_text = _call_claude_messages(api_key, claude_model, system, user_prompt)
        raw_text = _strip_json_blob(raw_text)
        parsed = json.loads(raw_text)
        if not isinstance(parsed, dict):
            raise ValueError("Réponse non-objet")
        bp = parsed.get("bonus_points", 0)
        bonus = max(0, min(5, int(bp)))
        logger.info(
            "Claude crédibilité %s|%s → bonus=%s",
            brand[:30],
            model[:40],
            bonus,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Claude crédibilité API échouée (%s|%s): %s — repli local", brand, model, exc)
        bonus = _local_credibility(
            brand=brand,
            model=model,
            price_min=price_min,
            price_max=price_max,
            price_avg=price_avg,
            nb_sources=nb_sources,
            groq_valid=groq_valid,
            groq_confidence=groq_confidence,
        )
        src = "local"

    _set_cache(cache_key, bonus)
    return _credibility_result_dict(
        enabled=True,
        claude_bonus=bonus,
        groq_valid=groq_valid,
        groq_confidence=groq_confidence,
        source=src,
    )


def get_claude_credibility_bonus(**kwargs: Any) -> int | None:
    """Extrait claude_bonus (0–5) pour confidence_scorer / main.py."""
    out = analyze_credibility(**kwargs)
    if not isinstance(out, dict) or not out.get("enabled"):
        return None
    b = out.get("claude_bonus")
    if b is None:
        return None
    return max(0, min(5, int(b)))


__all__ = [
    "analyze_credibility",
    "claude_credibility_enabled",
    "get_claude_credibility_bonus",
    "init_claude_cache",
]
