"""
File d'exécution du refresh marché FR via Celery.

- La tâche `refresh_fr_market` exécute le même pipeline que l'ancien worker thread
  (`run_fr_market_full`), puis invalide les caches HTTP / tracker.
- L'API FastAPI enfile le travail avec `refresh_fr_market.delay()` (non bloquant).
"""

from __future__ import annotations

import logging
from typing import Any

from celery_app import app as celery_app

logger = logging.getLogger(__name__)

# Dernier task_id Celery connu (manuel ou planifié), pour corrélation /api/refresh/status
_last_refresh_job_id: str | None = None


def _post_job_hook() -> None:
    """Après un refresh réussi ou non : caches + CSV préchargés alignés avec le disque."""
    try:
        from app.central_cache import invalidate_dashboard_keys

        invalidate_dashboard_keys()
    except Exception as exc:  # noqa: BLE001
        logger.debug("post_job invalidate_dashboard_keys: %s", exc)
    try:
        from app.analytics.tracker import clear_tracker_caches

        clear_tracker_caches()
    except Exception as exc:  # noqa: BLE001
        logger.debug("post_job clear_tracker_caches: %s", exc)
    try:
        from app.csv_preload import reload_after_market_update

        reload_after_market_update()
    except Exception as exc:  # noqa: BLE001
        logger.debug("post_job csv reload: %s", exc)
    try:
        from app.app import _clear_app_memory_caches

        _clear_app_memory_caches()
    except Exception as exc:  # noqa: BLE001
        logger.debug("post_job app caches: %s", exc)


@celery_app.task(name="refresh_fr_market", bind=True)
def refresh_fr_market(self) -> dict[str, Any]:
    """
    Tâche Celery : refresh complet FR (scraping + agrégation) puis invalidation caches.

    Le statut `fr_update_status.json` est mis à jour par `run_fr_market_full` dans le worker.
    """
    from app.scheduler import run_fr_market_full

    try:
        run_fr_market_full()
    finally:
        try:
            _post_job_hook()
        except Exception:  # noqa: BLE001
            logger.exception("post_job_hook après refresh Celery")
    return {"ok": True, "task_id": getattr(self.request, "id", None)}


def fr_refresh_is_running() -> bool:
    """True si le fichier de statut indique un refresh en cours (pipeline actif)."""
    try:
        from app.scheduler import get_last_refresh_status

        return bool(get_last_refresh_status().get("running"))
    except Exception:  # noqa: BLE001
        return False


def note_refresh_job_id(job_id: str) -> None:
    """Mémorise le dernier Celery task_id pour l'endpoint de statut."""
    global _last_refresh_job_id
    _last_refresh_job_id = job_id


def get_api_refresh_status() -> dict[str, Any]:
    """
    Charge utile pour GET /api/refresh/status.

    Combine ``fr_update_status.json`` (SQLite/cache inchangés côté API) avec l’état Celery
    du dernier task_id connu (broker Redis).
    """
    from app.scheduler import get_last_refresh_status

    base = get_last_refresh_status()
    out: dict[str, Any] = {
        "refresh_in_progress": bool(base.get("running")),
        "last_start_at": str(base.get("last_start_at") or ""),
        "last_end_at": str(base.get("last_end_at") or ""),
        "last_success": base.get("last_success"),
        "last_message": str(base.get("last_message") or ""),
        "updated_at": str(base.get("updated_at") or ""),
        "job_id": _last_refresh_job_id,
        # Résumé explicite pour les clients (état du dernier cycle de refresh)
        "last_refresh": {
            "in_progress": bool(base.get("running")),
            "ended_at": str(base.get("last_end_at") or ""),
            "success": base.get("last_success"),
            "message": str(base.get("last_message") or ""),
        },
    }
    jid = _last_refresh_job_id
    if jid:
        try:
            from celery.result import AsyncResult

            ar = AsyncResult(jid, app=celery_app)
            out["celery_state"] = ar.state
            out["celery_ready"] = ar.ready()
            if ar.successful():
                out["celery_result"] = ar.result
            elif ar.failed():
                out["celery_error"] = str(ar.result) if ar.result else None
        except Exception as exc:  # noqa: BLE001
            out["celery_meta_error"] = str(exc)
    return out


def dispatch_fr_refresh_celery() -> dict[str, Any]:
    """
    Enfile `refresh_fr_market.delay()` avec les mêmes garde-fous que l'API.
    Utilisé par `trigger_manual_fr_refresh` dans le scheduler.
    """
    from datetime import datetime

    if fr_refresh_is_running():
        logger.info("[REFRESH] manual launch blocked: refresh_deja_en_cours")
        return {"accepted": False, "reason": "refresh_deja_en_cours"}
    try:
        async_result = refresh_fr_market.delay()
    except Exception as exc:  # noqa: BLE001
        logger.exception("dispatch_fr_refresh_celery: %s", exc)
        return {"accepted": False, "error": "celery_broker_unavailable", "detail": str(exc)}
    note_refresh_job_id(async_result.id)
    return {
        "accepted": True,
        "job_id": async_result.id,
        "message": "refresh_fr_accepte",
        "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def submit_scheduled_fr_market() -> None:
    """
    Appelé par APScheduler (cron) : enfile une tâche Celery sans bloquer le scheduler.
    Si un refresh est déjà marqué en cours sur disque, on évite d'en empiler un second.
    """
    if fr_refresh_is_running():
        logger.info("Refresh FR planifié ignoré : déjà en cours (statut fichier)")
        return
    try:
        async_result = refresh_fr_market.delay()
        note_refresh_job_id(async_result.id)
        logger.info("Refresh FR planifié enfile Celery task_id=%s", async_result.id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Impossible d'enfiler refresh_fr_market (scheduler): %s", exc)
