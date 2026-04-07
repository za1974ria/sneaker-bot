"""APScheduler background scheduler for FR-only scraping."""

from __future__ import annotations

import logging
import json
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Lock
from typing import Final

from apscheduler.schedulers.background import BackgroundScheduler

from scrapers.aggregator import rebuild_fr_sources_csv, run_market


PROJECT_DIR: Final[Path] = Path(__file__).resolve().parent
LOG_DIR: Final[Path] = PROJECT_DIR / "logs"
LOG_PATH: Final[Path] = LOG_DIR / "scraper.log"
DATA_DIR: Final[Path] = PROJECT_DIR / "data"
FR_UPDATE_STATUS_PATH: Final[Path] = DATA_DIR / "fr_update_status.json"


_scheduler: BackgroundScheduler | None = None
_started = False
_guard_lock: Lock = Lock()


def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("scraper.scheduler")
    logger.setLevel(logging.INFO)

    # Avoid adding duplicate handlers on reload/import.
    if logger.handlers:
        return logger

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    handler = RotatingFileHandler(
        LOG_PATH,
        maxBytes=5 * 1024 * 1024,  # 5MB
        backupCount=3,
        encoding="utf-8",
    )
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    return logger


logger = _setup_logger()


def _write_fr_update_status(
    *,
    running: bool,
    last_start_at: str | None = None,
    last_end_at: str | None = None,
    last_success: bool | None = None,
    last_message: str | None = None,
) -> None:
    """
    État minimal du refresh FR pour exposition UI/API.
    """
    payload: dict[str, object] = {}
    if FR_UPDATE_STATUS_PATH.is_file():
        try:
            payload = json.loads(FR_UPDATE_STATUS_PATH.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
    payload["running"] = bool(running)
    if last_start_at is not None:
        payload["last_start_at"] = last_start_at
    if last_end_at is not None:
        payload["last_end_at"] = last_end_at
    if last_success is not None:
        payload["last_success"] = bool(last_success)
    if last_message is not None:
        payload["last_message"] = last_message
    payload["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    FR_UPDATE_STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    FR_UPDATE_STATUS_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _run_fr_market() -> None:
    start_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _write_fr_update_status(running=True, last_start_at=start_at, last_message="refresh_en_cours")
    try:
        run_market("FR")
        logger.info("market=FR scraped/updated ok")
        _write_fr_update_status(
            running=False,
            last_end_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            last_success=True,
            last_message="refresh_termine",
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("market=FR scraping failed: %s", e)
        _write_fr_update_status(
            running=False,
            last_end_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            last_success=False,
            last_message=f"refresh_erreur: {e}",
        )


def _run_fr_hourly_refresh() -> None:
    """
    Refresh horaire rapide: reconstruction du CSV sources avec un sous-ensemble
    stable multi-shops (8 max) pour conserver un minimum de diversité.
    """
    start_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _write_fr_update_status(running=True, last_start_at=start_at, last_message="refresh_horaire_en_cours")
    try:
        rows = rebuild_fr_sources_csv(max_sites=9, workers=4)
        logger.info("market=FR hourly sources refresh ok rows=%d", rows)
        _write_fr_update_status(
            running=False,
            last_end_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            last_success=True,
            last_message="refresh_horaire_termine",
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("market=FR hourly sources refresh failed: %s", e)
        _write_fr_update_status(
            running=False,
            last_end_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            last_success=False,
            last_message=f"refresh_horaire_erreur: {e}",
        )


def _run_aegis_daily() -> None:
    """
    AEGIS (Claude + web) : une passe quotidienne sur market_fr.csv, hors scraping.
    Réduit la consommation de tokens vs un déclenchement après chaque agrégat.
    """
    try:
        from app.aegis_verifier import (
            aegis_enabled,
            read_market_fr_rows_for_aegis,
            run_aegis_after_market_rows,
        )

        if not aegis_enabled():
            return
        rows = read_market_fr_rows_for_aegis()
        if not rows:
            return
        rep = run_aegis_after_market_rows(rows, market="FR", ignore_cache_ttl=False)
        logger.info(
            "aegis_daily processed=%s skipped_cache=%s errors=%s",
            rep.get("processed"),
            rep.get("skipped_cache"),
            rep.get("errors"),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("aegis_daily skip: %s", e)


def _run_google_verification() -> None:
    """Google Shopping (Playwright) : échantillon quotidien, ne bloque jamais le scheduler."""
    try:
        from app.google_shopping_verifier import init_google_cache, run_google_verification_batch

        init_google_cache()
        logger.info("Google Shopping: vérification (batch max 5 modèles)...")
        result = run_google_verification_batch(sample_size=5)
        logger.info(
            "Google Shopping terminé: %s OK | %s suspects | %s invalides | %s inconnus (total=%s)",
            result.get("ok"),
            result.get("suspect"),
            result.get("invalid"),
            result.get("unknown"),
            result.get("total"),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Google verify: %s", e)


def _run_fr_sources_nightly() -> None:
    """
    Enrichissement nocturne des sources boutiques:
    Courir + Foot Locker + Snipes + Sports Direct.
    """
    try:
        _write_fr_update_status(
            running=True,
            last_start_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            last_message="refresh_nocturne_en_cours",
        )
        rows = rebuild_fr_sources_csv(max_sites=9)
        logger.info("market=FR sources nightly rebuilt ok rows=%d", rows)
        _write_fr_update_status(
            running=False,
            last_end_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            last_success=True,
            last_message="refresh_nocturne_termine",
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("market=FR sources nightly failed: %s", e)
        _write_fr_update_status(
            running=False,
            last_end_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            last_success=False,
            last_message=f"refresh_nocturne_erreur: {e}",
        )


def _is_fr_data_stale(max_age_minutes: int = 70) -> bool:
    """
    Données FR considérées périmées si les CSV n'ont pas été modifiés récemment.
    """
    candidates = [
        DATA_DIR / "market_fr.csv",
        DATA_DIR / "market_fr_sources.csv",
    ]
    mtimes: list[float] = []
    for p in candidates:
        if p.is_file():
            try:
                mtimes.append(p.stat().st_mtime)
            except OSError:
                continue
    if not mtimes:
        return True
    last_ts = max(mtimes)
    last_dt = datetime.fromtimestamp(last_ts)
    return (datetime.now() - last_dt) > timedelta(minutes=max_age_minutes)


def start_scheduler() -> None:
    """
    Start APScheduler exactly once (guarded).
    Intended to be called from FastAPI startup.
    """
    global _scheduler, _started
    with _guard_lock:
        if _started:
            return

        _scheduler = BackgroundScheduler()
        # Reset d'état au boot pour éviter un badge "en cours" figé après crash/restart.
        _write_fr_update_status(
            running=False,
        )

        # Refresh partiel toutes les 30 minutes (cadence stable, coûts maîtrisés).
        _scheduler.add_job(
            _run_fr_hourly_refresh,
            trigger="cron",
            minute="*/30",
            id="scraper_interval",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        # Job enrichissement sources boutiques la nuit (03:30 heure serveur).
        _scheduler.add_job(
            _run_fr_sources_nightly,
            trigger="cron",
            hour=3,
            minute=30,
            id="sources_nightly",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        # Refresh complet FR (market_fr.csv + sources) — toutes les 2 h.
        _scheduler.add_job(
            run_scheduled_fr_market,
            trigger="cron",
            minute=0,
            hour="*/2",
            id="run_market_fr_hourly",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        # Vérification AEGIS (Claude web) — 1× par jour à 8h00 (heure serveur).
        _scheduler.add_job(
            _run_aegis_daily,
            trigger="cron",
            hour=8,
            minute=0,
            id="aegis_daily",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        # Vérification Google Shopping (Playwright) — 1× par jour à 9h00 (heure serveur).
        _scheduler.add_job(
            _run_google_verification,
            trigger="cron",
            hour=9,
            minute=0,
            id="google_shopping_verify",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        # Startup refresh rapide si données trop anciennes (safe, non bloquant).
        if _is_fr_data_stale():
            _scheduler.add_job(
                _run_fr_hourly_refresh,
                trigger="date",
                run_date=datetime.now() + timedelta(seconds=20),
                id="startup_hourly_refresh_if_stale",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info("Startup hourly refresh scheduled (stale FR data detected)")

        _scheduler.start()
        _started = True
        logger.info(
            "Scraper scheduler started (partial sources every 30min, full run_market every 2h @:00, nightly @03:30, aegis @08:00, google @09:00)"
        )


def run_scheduled_fr_market() -> None:
    """
    Point d'entrée public pour déclencher run_market("FR") avec mise à jour fr_update_status.json.
    Utilisé par les jobs planifiés et les triggers manuels (ex. main.py).
    """
    _run_fr_market()


def shutdown_scheduler() -> None:
    """Arrêt propre du scheduler (ex. shutdown FastAPI)."""
    global _scheduler, _started
    with _guard_lock:
        if _scheduler is not None:
            try:
                _scheduler.shutdown(wait=False)
            except Exception:  # noqa: BLE001
                pass
            _scheduler = None
        _started = False


def scheduler_is_running() -> bool:
    """True si le BackgroundScheduler est démarré."""
    if _scheduler is None:
        return False
    return bool(getattr(_scheduler, "running", False))

