"""
Historique des prix sneakers — SneakerBot
Base SQLite : data/price_history.db
Rétention : 30 jours glissants
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = _ROOT / "data" / "price_history.db"
RETENTION_DAYS = 30

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS price_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    brand       TEXT    NOT NULL,
    model       TEXT    NOT NULL,
    market      TEXT    NOT NULL DEFAULT 'FR',
    price_min   REAL    NOT NULL,
    price_max   REAL    NOT NULL,
    price_avg   REAL    NOT NULL,
    nb_sources  INTEGER NOT NULL DEFAULT 1,
    groq_valid  INTEGER,
    recorded_at TEXT    NOT NULL
);
"""

CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_ph_brand_model ON price_history(brand, model);",
    "CREATE INDEX IF NOT EXISTS idx_ph_recorded_at ON price_history(recorded_at);",
]


@contextmanager
def _db() -> Any:
    """Connexion SQLite avec WAL (check_same_thread=False pour usage multi-threads)."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Initialise la base et les index."""
    with _db() as conn:
        conn.execute(CREATE_TABLE)
        for idx in CREATE_INDEXES:
            conn.execute(idx)
    logger.info("price_history.db initialisée")


def record_snapshot(
    brand: str,
    model: str,
    price_min: float,
    price_max: float,
    price_avg: float,
    nb_sources: int = 1,
    groq_valid: Optional[bool] = None,
    market: str = "FR",
    *,
    recorded_at: Optional[str] = None,
) -> None:
    """
    Enregistre un snapshot de prix.
    Anti-doublon : même brand/model/market et snapshot dans les 30 dernières minutes → skip.
    Si ``recorded_at`` est fourni (ISO UTC), utilisé tel quel et la déduplication est ignorée
    (tests / backfill).
    """
    brand = (brand or "").strip()
    model = (model or "").strip()
    market = (market or "FR").strip().upper() or "FR"
    if not brand or not model:
        return

    use_ts = recorded_at or datetime.now(timezone.utc).isoformat()

    with _db() as conn:
        if recorded_at is None:
            thirty_min_ago = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
            existing = conn.execute(
                """
                SELECT id FROM price_history
                WHERE brand=? AND model=? AND market=?
                AND recorded_at > ?
                LIMIT 1
                """,
                (brand, model, market, thirty_min_ago),
            ).fetchone()
            if existing:
                logger.debug("Skip doublon %s %s — snapshot récent", brand, model)
                return

        gv_sql: int | None
        if groq_valid is None:
            gv_sql = None
        else:
            gv_sql = 1 if groq_valid else 0

        conn.execute(
            """
            INSERT INTO price_history
            (brand, model, market, price_min, price_max, price_avg,
             nb_sources, groq_valid, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                brand,
                model,
                market,
                float(price_min),
                float(price_max),
                float(price_avg),
                int(max(1, nb_sources)),
                gv_sql,
                use_ts,
            ),
        )


def get_history(
    brand: str,
    model: str,
    days: int = 30,
    market: str = "FR",
) -> list[dict[str, Any]]:
    """Historique trié par date croissante."""
    brand = (brand or "").strip()
    model = (model or "").strip()
    market = (market or "FR").strip().upper() or "FR"
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    with _db() as conn:
        rows = conn.execute(
            """
            SELECT brand, model, market,
                   price_min, price_max, price_avg,
                   nb_sources, groq_valid, recorded_at
            FROM price_history
            WHERE brand=? AND model=? AND market=?
            AND recorded_at >= ?
            ORDER BY recorded_at ASC
            """,
            (brand, model, market, since),
        ).fetchall()

    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        gv = d.get("groq_valid")
        if gv is None:
            d["groq_valid"] = None
        else:
            d["groq_valid"] = bool(int(gv))
        out.append(d)
    return out


def get_price_trend(brand: str, model: str, days: int = 30) -> dict[str, Any]:
    history = get_history(brand, model, days=days)

    if not history:
        return {"trend": "inconnu", "change_pct": 0.0, "nb_snapshots": 0}

    if len(history) == 1:
        h = history[0]
        return {
            "trend": "stable",
            "change_pct": 0.0,
            "min_30d": h["price_min"],
            "max_30d": h["price_max"],
            "avg_30d": h["price_avg"],
            "nb_snapshots": 1,
            "first_price": h["price_avg"],
            "last_price": h["price_avg"],
        }

    first = float(history[0]["price_avg"])
    last = float(history[-1]["price_avg"])
    all_avgs = [float(h["price_avg"]) for h in history]

    change_pct = ((last - first) / first * 100) if first > 0 else 0.0

    if change_pct > 3:
        trend = "hausse"
    elif change_pct < -3:
        trend = "baisse"
    else:
        trend = "stable"

    return {
        "trend": trend,
        "change_pct": round(change_pct, 2),
        "min_30d": min(float(h["price_min"]) for h in history),
        "max_30d": max(float(h["price_max"]) for h in history),
        "avg_30d": round(sum(all_avgs) / len(all_avgs), 2),
        "nb_snapshots": len(history),
        "first_price": round(first, 2),
        "last_price": round(last, 2),
    }


def purge_old_records() -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat()
    with _db() as conn:
        cur = conn.execute("DELETE FROM price_history WHERE recorded_at < ?", (cutoff,))
        deleted = cur.rowcount
    if deleted:
        logger.info("Purge historique : %s entrées supprimées", deleted)
    return int(deleted) if deleted is not None and deleted >= 0 else 0


def get_global_stats() -> dict[str, Any]:
    with _db() as conn:
        total = int(conn.execute("SELECT COUNT(*) as c FROM price_history").fetchone()["c"])
        models = int(
            conn.execute("SELECT COUNT(DISTINCT brand||'|'||model) as c FROM price_history").fetchone()["c"]
        )
        oldest_row = conn.execute("SELECT MIN(recorded_at) as d FROM price_history").fetchone()
        oldest = oldest_row["d"] if oldest_row else None
    return {
        "total_snapshots": total,
        "models_tracked": models,
        "oldest_record": oldest,
    }
