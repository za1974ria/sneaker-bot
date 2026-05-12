"""
SerpAPI Monthly Budget Cap — PA3
Compteur réel d'appels HTTP SerpAPI, persisté en JSON.
Politique (% utilisé du quota mensuel) :
  0–70%  → normal
  70–85% → prudent
  85–95% → essential   (low_sources + suspect_min + premium_low_confidence seulement)
  95–100% → stop       (hard stop : zéro appel)
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_BUDGET_PATH = _DATA_DIR / "serpapi_budget.json"
_LOCK = threading.Lock()
_DEFAULT_MONTHLY_CAP = 1_000


# ── Helpers persistence ────────────────────────────────────────────────────────

def _current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _read_raw() -> dict[str, Any]:
    try:
        if _BUDGET_PATH.is_file():
            data = json.loads(_BUDGET_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _write_raw(data: dict[str, Any]) -> None:
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        _BUDGET_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("serpapi_budget write failed: %s", exc)


def _get_or_init() -> dict[str, Any]:
    """Charge le budget du mois courant. Reset automatique si nouveau mois."""
    raw = _read_raw()
    month = _current_month()
    if raw.get("month") != month:
        # Nouveau mois — archiver l'ancien dans "history", reset compteur
        history: list[dict[str, Any]] = list(raw.get("history") or [])
        if raw.get("month"):
            history.append({
                "month": raw["month"],
                "calls": raw.get("calls", 0),
                "daily": raw.get("daily", {}),
            })
        history = history[-6:]  # garder 6 mois
        raw = {
            "month": month,
            "calls": 0,
            "daily": {},
            "last_updated": "",
            "history": history,
        }
    return raw


# ── API publique ───────────────────────────────────────────────────────────────

def increment_call_count(n: int = 1) -> int:
    """
    Incrémente le compteur d'appels HTTP réels SerpAPI.
    À appeler AVANT chaque fetch_url_get() vers serpapi.com.
    Retourne le nouveau total mensuel.
    """
    with _LOCK:
        data = _get_or_init()
        data["calls"] = int(data.get("calls") or 0) + n
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily: dict[str, int] = dict(data.get("daily") or {})
        daily[today] = int(daily.get(today) or 0) + n
        data["daily"] = daily
        data["last_updated"] = datetime.now(timezone.utc).isoformat()
        _write_raw(data)
        total = int(data["calls"])
    logger.debug("serpapi_budget: appel enregistré total_mois=%d", total)
    return total


def get_budget_status() -> dict[str, Any]:
    """
    Retourne les métriques complètes pour le health endpoint.
    Champs : serpapi_calls_this_month, serpapi_daily_average,
             serpapi_budget_status, serpapi_budget_remaining,
             serpapi_hard_stop, serpapi_monthly_cap.
    """
    cap = int(os.getenv("SERPAPI_MONTHLY_BUDGET", str(_DEFAULT_MONTHLY_CAP)))

    with _LOCK:
        data = _get_or_init()

    calls = int(data.get("calls") or 0)
    daily: dict[str, int] = dict(data.get("daily") or {})
    month = data.get("month", _current_month())

    # Moyenne journalière (jours avec au moins 1 appel)
    active_days = [v for v in daily.values() if v > 0]
    daily_avg = round(sum(active_days) / len(active_days), 1) if active_days else 0.0

    used_pct = round(calls / cap * 100, 1) if cap > 0 else 0.0
    remaining = max(0, cap - calls)
    remaining_pct = round(remaining / cap * 100, 1) if cap > 0 else 0.0

    # Politique budget
    env_mode = os.getenv("SERPAPI_BUDGET_MODE", "").strip().lower()
    override_active = env_mode in ("normal", "prudent", "essential", "stop")
    if override_active:
        mode = env_mode
    elif used_pct >= 95:
        mode = "stop"
    elif used_pct >= 85:
        mode = "essential"
    elif used_pct >= 70:
        mode = "prudent"
    else:
        mode = "normal"

    return {
        "month": month,
        "serpapi_calls_this_month": calls,
        "serpapi_monthly_cap": cap,
        "serpapi_used_pct": used_pct,
        "serpapi_budget_remaining": remaining,
        "serpapi_budget_remaining_pct": remaining_pct,
        "serpapi_daily_average": daily_avg,
        "serpapi_budget_status": mode,
        "serpapi_hard_stop": (mode == "stop"),
        "serpapi_override_active": override_active,
    }


def get_budget_mode() -> str:
    """
    Retourne le mode budget courant pour les décisions de gate.
    Rapide (lit le JSON une fois, pas de lock long).
    """
    return get_budget_status()["serpapi_budget_status"]
