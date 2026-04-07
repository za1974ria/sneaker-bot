"""
Vérification post-scraping des prix agrégés (marché FR) via Claude + recherche web.

- Inactif par défaut : activer avec AEGIS_VERIFIER=1 et ANTHROPIC_API_KEY.
- Journal SQLite : data/aegis_verifier.db (audit + anti-spam cache).
- Ne modifie pas market_fr.csv ; n’interrompt jamais le pipeline si l’API échoue.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DB_LOCK = threading.Lock()
_CLIENT_LOCK = threading.Lock()
_ANTHROPIC_CLIENT: Any = None
_ANTHROPIC_CLIENT_KEY: str | None = None

_DEFAULT_MODEL_WEB = "claude-haiku-4-5-20251001"
_FALLBACK_MODEL = "claude-haiku-4-5-20251001"
_WEB_SEARCH_BETA = "web-search-2025-03-05"


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    except Exception:
        pass


_load_dotenv()


def aegis_enabled() -> bool:
    if (os.getenv("DISABLE_AEGIS_VERIFIER") or "").strip().lower() in ("1", "true", "yes"):
        return False
    if (os.getenv("AEGIS_VERIFIER") or "").strip() != "1":
        return False
    return bool((os.getenv("ANTHROPIC_API_KEY") or "").strip())


def _db_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "aegis_verifier.db"


def init_aegis_db() -> None:
    p = _db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with _DB_LOCK:
        con = sqlite3.connect(p, timeout=30)
        try:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS aegis_checks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    brand TEXT NOT NULL,
                    model TEXT NOT NULL,
                    market TEXT NOT NULL,
                    price_min REAL,
                    price_max REAL,
                    price_avg REAL,
                    nb_sources INTEGER,
                    verdict TEXT NOT NULL,
                    confidence REAL,
                    summary TEXT,
                    raw_response TEXT,
                    used_web_search INTEGER NOT NULL DEFAULT 0,
                    dedupe_key TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )
            cols = {row[1] for row in con.execute("PRAGMA table_info(aegis_checks)")}
            if "dedupe_key" not in cols:
                con.execute(
                    "ALTER TABLE aegis_checks ADD COLUMN dedupe_key TEXT NOT NULL DEFAULT ''"
                )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_aegis_brand_model ON aegis_checks(brand, model)"
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_aegis_created ON aegis_checks(created_at)")
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_aegis_dedupe ON aegis_checks(dedupe_key, created_at)"
            )
            con.commit()
        finally:
            con.close()


def _cache_fingerprint(
    brand: str,
    model: str,
    pm: float,
    px: float,
    pa: float,
    nb: int,
) -> str:
    payload = f"{brand.strip()}|{model.strip()}|{pm:.1f}|{px:.1f}|{pa:.1f}|{nb}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _recently_checked(fp: str, ttl_hours: float) -> bool:
    if ttl_hours <= 0:
        return False
    init_aegis_db()
    cutoff = time.time() - ttl_hours * 3600.0
    cutoff_iso = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cutoff))
    with _DB_LOCK:
        con = sqlite3.connect(_db_path(), timeout=30)
        try:
            cur = con.execute(
                """
                SELECT 1 FROM aegis_checks
                WHERE dedupe_key = ? AND created_at >= ?
                LIMIT 1
                """,
                (fp, cutoff_iso),
            )
            return cur.fetchone() is not None
        finally:
            con.close()


def _insert_check(
    *,
    brand: str,
    model: str,
    market: str,
    price_min: float,
    price_max: float,
    price_avg: float,
    nb_sources: int,
    verdict: str,
    confidence: float,
    summary: str,
    raw_response: str,
    used_web_search: bool,
    fingerprint: str,
) -> None:
    init_aegis_db()
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    raw_trim = raw_response[:12000] if raw_response else ""
    with _DB_LOCK:
        con = sqlite3.connect(_db_path(), timeout=30)
        try:
            con.execute(
                """
                INSERT INTO aegis_checks (
                    brand, model, market, price_min, price_max, price_avg, nb_sources,
                    verdict, confidence, summary, raw_response, used_web_search, dedupe_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    brand[:120],
                    model[:200],
                    market[:8],
                    price_min,
                    price_max,
                    price_avg,
                    nb_sources,
                    verdict[:64],
                    confidence,
                    summary[:2000],
                    raw_trim,
                    1 if used_web_search else 0,
                    fingerprint,
                    now,
                ),
            )
            con.commit()
        finally:
            con.close()


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
        import anthropic

        _ANTHROPIC_CLIENT = anthropic.Anthropic(api_key=api_key)
        _ANTHROPIC_CLIENT_KEY = api_key
        return _ANTHROPIC_CLIENT


def _extract_text_from_message(msg: Any) -> str:
    parts: list[str] = []
    for block in getattr(msg, "content", []) or []:
        btype = getattr(block, "type", None) or (
            isinstance(block, dict) and block.get("type")
        )
        if btype == "text":
            txt = getattr(block, "text", None)
            if txt is None and isinstance(block, dict):
                txt = block.get("text")
            parts.append(str(txt or ""))
    return "".join(parts).strip()


def _call_claude_web_search(
    *,
    api_key: str,
    model: str,
    system: str,
    user_text: str,
) -> tuple[str, bool]:
    """
    Retourne (texte_brut_modèle, used_web_search).
    """
    client = _get_anthropic_client(api_key)
    if client is None:
        raise RuntimeError("Client Anthropic indisponible")

    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 4}]

    try:
        msg = client.beta.messages.create(
            model=model,
            max_tokens=1400,
            system=system,
            messages=[{"role": "user", "content": user_text}],
            tools=tools,
            betas=[_WEB_SEARCH_BETA],  # type: ignore[list-item]
        )
        text = _extract_text_from_message(msg)
        if not text.strip():
            raise ValueError("Réponse vide après web_search")
        return text, True
    except Exception as exc:  # noqa: BLE001
        logger.info("Aegis web_search indisponible ou refus API (%s) — repli sans web", exc)
        msg2 = client.messages.create(
            model=_FALLBACK_MODEL,
            max_tokens=900,
            system=system,
            messages=[{"role": "user", "content": user_text + "\n\n(NB: pas d'outil web — estime à partir de ta connaissance.)"}],
            temperature=0.2,
        )
        return _extract_text_from_message(msg2), False


def verify_row_claude_web(
    *,
    brand: str,
    model: str,
    market: str,
    price_min: float,
    price_max: float,
    price_avg: float,
    nb_sources: int,
    groq_valid: str | None = None,
    groq_confidence: str | None = None,
) -> dict[str, Any]:
    """
    Un appel Claude (web si possible) → dict normalisé verdict/confidence/summary.
    """
    if not aegis_enabled():
        return {
            "ok": False,
            "skipped": True,
            "reason": "aegis_disabled",
            "verdict": "unknown",
            "confidence": 0.0,
            "summary": "",
            "used_web_search": False,
        }

    api_key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
    model_web = (os.getenv("AEGIS_CLAUDE_MODEL") or _DEFAULT_MODEL_WEB).strip()

    gv = (groq_valid or "").strip() or "N/A"
    gc = (groq_confidence or "").strip() or "N/A"

    system = (
        "Tu es un analyste prix sneakers neuf, marché France (e-commerce / retailers). "
        "Tu utilises la recherche web quand elle est disponible pour comparer les prix publics récents. "
        "Réponds UNIQUEMENT par un JSON valide, sans markdown ni texte hors JSON."
    )
    user_prompt = f"""Données agrégées SneakerBot (scraping multi-sites FR) :
- Marque : {brand}
- Modèle : {model}
- Marché : {market}
- Prix min / moyen / max (EUR) : {price_min:.2f} / {price_avg:.2f} / {price_max:.2f}
- Nombre de sources agrégées : {nb_sources}
- Groq validation (CSV) : {gv}, confidence : {gc}

Tâche :
1) Vérifie si la fourchette et la moyenne sont plausibles pour ce modèle en France aujourd'hui (prix neuf).
2) verdict : exactement une de ces valeurs : "aligned" | "plausible" | "suspicious" | "insufficient_data"
3) confidence : nombre entre 0 et 1
4) summary : une à trois phrases en français (mentionne brièvement l'écart éventuel vs ce que tu vois en ligne si pertinent)
5) web_notes : courte phrase sur l'utilité de la recherche web (ou "n/a")

Format JSON strict :
{{"verdict":"...","confidence":0.0,"summary":"...","web_notes":"..."}}"""

    used_web = False
    raw_text = ""
    try:
        raw_text, used_web = _call_claude_web_search(
            api_key=api_key,
            model=model_web,
            system=system,
            user_text=user_prompt,
        )
        raw_text = _strip_json_blob(raw_text)
        parsed = json.loads(raw_text)
        if not isinstance(parsed, dict):
            raise ValueError("Réponse non-objet")
        verdict = str(parsed.get("verdict") or "unknown").lower().strip()
        if verdict not in ("aligned", "plausible", "suspicious", "insufficient_data"):
            verdict = "unknown"
        conf = float(parsed.get("confidence") or 0.0)
        conf = max(0.0, min(1.0, conf))
        summary = str(parsed.get("summary") or "").strip()
        wn = str(parsed.get("web_notes") or "").strip()
        if wn:
            summary = f"{summary} [{wn}]".strip()
        return {
            "ok": True,
            "skipped": False,
            "verdict": verdict,
            "confidence": conf,
            "summary": summary[:1500],
            "used_web_search": used_web,
            "raw_json": json.dumps(parsed, ensure_ascii=False),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("Aegis verify échoué (%s %s): %s", brand, model, exc)
        return {
            "ok": False,
            "skipped": False,
            "reason": str(exc)[:200],
            "verdict": "error",
            "confidence": 0.0,
            "summary": "",
            "used_web_search": used_web,
            "raw_json": raw_text[:4000] if raw_text else "",
        }


def run_aegis_after_market_rows(
    rows: list[dict[str, Any]],
    *,
    market: str = "FR",
    ignore_cache_ttl: bool = False,
) -> dict[str, Any]:
    """
    Vérifie un sous-ensemble de lignes (quota + cache TTL) — job quotidien scheduler
    ou bouton « Vérifier maintenant » ; plus déclenché automatiquement après chaque scraping.
    ``ignore_cache_ttl=True`` : ignore le cache (ex. bouton « Vérifier maintenant »).
    """
    out: dict[str, Any] = {
        "enabled": aegis_enabled(),
        "processed": 0,
        "skipped_cache": 0,
        "errors": 0,
    }
    if not aegis_enabled() or not rows:
        return out

    try:
        max_n = int((os.getenv("AEGIS_MAX_PER_RUN") or "5").strip() or "5")
    except ValueError:
        max_n = 5
    max_n = max(1, min(80, max_n))

    try:
        ttl_h = float((os.getenv("AEGIS_CACHE_TTL_HOURS") or "72").strip() or "72")
    except ValueError:
        ttl_h = 72.0

    init_aegis_db()
    processed = 0
    skipped = 0
    errors = 0

    for r in rows:
        if processed >= max_n:
            break
        b = str(r.get("brand") or "").strip()
        m = str(r.get("model") or "").strip()
        if not b or not m:
            continue
        try:
            pm = float(r.get("price_min"))
            px = float(r.get("price_max"))
            pa = float(r.get("price_avg"))
        except (TypeError, ValueError):
            continue
        try:
            nb = int(float(str(r.get("nb_sources") or "1").replace(",", ".")))
        except (TypeError, ValueError):
            nb = 1
        nb = max(1, nb)

        fp = _cache_fingerprint(b, m, pm, px, pa, nb)
        if not ignore_cache_ttl and _recently_checked(fp, ttl_h):
            skipped += 1
            continue

        gv = str(r.get("groq_valid") or "").strip() or None
        gc = str(r.get("groq_confidence") or "").strip() or None

        res = verify_row_claude_web(
            brand=b,
            model=m,
            market=market,
            price_min=pm,
            price_max=px,
            price_avg=pa,
            nb_sources=nb,
            groq_valid=gv,
            groq_confidence=gc,
        )

        verdict = str(res.get("verdict") or "unknown")
        conf = float(res.get("confidence") or 0.0)
        summary = str(res.get("summary") or "")
        raw_store = str(res.get("raw_json") or res.get("reason") or "")

        try:
            _insert_check(
                brand=b,
                model=m,
                market=market,
                price_min=pm,
                price_max=px,
                price_avg=pa,
                nb_sources=nb,
                verdict=verdict,
                confidence=conf,
                summary=summary,
                raw_response=raw_store or "{}",
                used_web_search=bool(res.get("used_web_search")),
                fingerprint=fp,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Aegis insert DB skip %s %s: %s", b, m, exc)
            errors += 1
            continue

        processed += 1
        if res.get("ok") and verdict in ("suspicious",):
            logger.warning(
                "AEGIS suspicious %s | %s | avg=%.2f | conf=%.2f | %s",
                b,
                m,
                pa,
                conf,
                summary[:120],
            )
        elif res.get("ok"):
            logger.info(
                "AEGIS %s | %s | %s | web=%s",
                verdict,
                b[:20],
                m[:28],
                res.get("used_web_search"),
            )
        else:
            errors += 1

    out["processed"] = processed
    out["skipped_cache"] = skipped
    out["errors"] = errors
    return out


def get_recent_checks(limit: int = 30) -> list[dict[str, Any]]:
    init_aegis_db()
    limit = max(1, min(200, int(limit)))
    with _DB_LOCK:
        con = sqlite3.connect(_db_path(), timeout=30)
        try:
            con.row_factory = sqlite3.Row
            cur = con.execute(
                """
                SELECT id, brand, model, market, price_min, price_max, price_avg, nb_sources,
                       verdict, confidence, summary, used_web_search, created_at
                FROM aegis_checks
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
            return [dict(row) for row in cur.fetchall()]
        finally:
            con.close()


def read_market_fr_rows_for_aegis() -> list[dict[str, Any]]:
    """Lit ``data/market_fr.csv`` pour relancer une passe AEGIS (hors scraping)."""
    import csv

    p = Path(__file__).resolve().parent.parent / "data" / "market_fr.csv"
    if not p.is_file():
        return []
    try:
        with p.open(encoding="utf-8", newline="") as f:
            return [dict(r) for r in csv.DictReader(f)]
    except OSError:
        return []


def run_aegis_verify_now_from_disk() -> dict[str, Any]:
    """Bouton dashboard : relit le CSV et exécute AEGIS (ignore le TTL cache)."""
    rows = read_market_fr_rows_for_aegis()
    base: dict[str, Any] = {
        "ok": False,
        "enabled": aegis_enabled(),
        "processed": 0,
        "skipped_cache": 0,
        "errors": 0,
        "message": "",
    }
    if not aegis_enabled():
        base["message"] = "AEGIS désactivé (AEGIS_VERIFIER=1 requis)"
        return base
    if not rows:
        base["message"] = "market_fr.csv absent ou vide"
        return base
    rep = run_aegis_after_market_rows(rows, market="FR", ignore_cache_ttl=True)
    rep["ok"] = True
    rep["message"] = "Vérifications exécutées"
    return rep


__all__ = [
    "aegis_enabled",
    "get_recent_checks",
    "init_aegis_db",
    "read_market_fr_rows_for_aegis",
    "run_aegis_after_market_rows",
    "run_aegis_verify_now_from_disk",
    "verify_row_claude_web",
]
