"""
Alertes de variation de prix (> 10 %) — SQLite ``data/price_alerts.db``.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = _ROOT / "data" / "price_alerts.db"
ALERT_THRESHOLD_PCT = 10.0

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS price_alerts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    brand        TEXT NOT NULL,
    model        TEXT NOT NULL,
    alert_type   TEXT NOT NULL,
    old_price    REAL NOT NULL,
    new_price    REAL NOT NULL,
    change_pct   REAL NOT NULL,
    detected_at  TEXT NOT NULL,
    seen         INTEGER NOT NULL DEFAULT 0
);
"""

INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_pa_seen ON price_alerts(seen);"


@contextmanager
def _db() -> Any:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_alerts_db() -> None:
    """Crée la table alertes si besoin."""
    with _db() as conn:
        conn.execute(CREATE_SQL)
        conn.execute(INDEX_SQL)
    logger.info("price_alerts.db prête")


def check_price_alerts(brand: str, model: str, new_price_avg: float) -> None:
    """
    Compare ``new_price_avg`` au dernier snapshot historique (avant le run actuel).
    Si variation > 10 %, insère une alerte.
    """
    brand = (brand or "").strip()
    model = (model or "").strip()
    if not brand or not model:
        return
    try:
        new_p = float(new_price_avg)
    except (TypeError, ValueError):
        return
    if new_p <= 0:
        return

    try:
        from app.price_history import get_history
    except Exception as e:  # noqa: BLE001
        logger.debug("check_price_alerts: get_history indisponible %s", e)
        return

    try:
        hist = get_history(brand, model, days=30, market="FR")
    except Exception as e:  # noqa: BLE001
        logger.debug("check_price_alerts: get_history %s %s: %s", brand, model, e)
        return

    if len(hist) < 2:
        return

    # Dernier = snapshot tout juste enregistré ; avant-dernier = ancien prix de référence
    old_p = float(hist[-2]["price_avg"])
    if old_p <= 0:
        return

    change_pct = (new_p - old_p) / old_p * 100.0
    if abs(change_pct) < ALERT_THRESHOLD_PCT:
        return

    alert_type = "hausse" if change_pct > 0 else "baisse"
    detected = datetime.now(timezone.utc).isoformat()
    emoji = "📈" if alert_type == "hausse" else "📉"
    logger.info(
        "%s ALERTE %s : %s %s %.2f\u00a0€ → %.2f\u00a0€ (%+.1f%%)",
        emoji,
        alert_type.upper(),
        brand,
        model,
        old_p,
        new_p,
        change_pct,
    )

    try:
        with _db() as conn:
            conn.execute(
                """
                INSERT INTO price_alerts
                (brand, model, alert_type, old_price, new_price, change_pct, detected_at, seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (brand, model, alert_type, old_p, new_p, round(change_pct, 4), detected),
            )
    except Exception as e:  # noqa: BLE001
        logger.debug("check_price_alerts insert: %s", e)


def get_recent_alerts(limit: int = 20, unseen_only: bool = False) -> list[dict[str, Any]]:
    limit = max(1, min(200, int(limit)))
    q = """
        SELECT id, brand, model, alert_type, old_price, new_price, change_pct, detected_at, seen
        FROM price_alerts
    """
    if unseen_only:
        q += " WHERE seen = 0"
    q += " ORDER BY id DESC LIMIT ?"

    with _db() as conn:
        rows = conn.execute(q, (limit,)).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["seen"] = bool(d.get("seen"))
        out.append(d)
    return out


def mark_alerts_seen(alert_ids: Optional[list[int]] = None) -> int:
    """Marque comme vues : ids listés, ou toutes si None."""
    with _db() as conn:
        if alert_ids:
            placeholders = ",".join("?" * len(alert_ids))
            cur = conn.execute(
                f"UPDATE price_alerts SET seen = 1 WHERE id IN ({placeholders})",
                tuple(int(x) for x in alert_ids),
            )
        else:
            cur = conn.execute("UPDATE price_alerts SET seen = 1")
        return int(cur.rowcount or 0)


def get_alerts_count() -> dict[str, int]:
    with _db() as conn:
        total = int(conn.execute("SELECT COUNT(*) AS c FROM price_alerts").fetchone()["c"])
        unseen = int(
            conn.execute("SELECT COUNT(*) AS c FROM price_alerts WHERE seen = 0").fetchone()["c"]
        )
    return {"total": total, "unseen": unseen}


__all__ = [
    "ALERT_THRESHOLD_PCT",
    "check_price_alerts",
    "get_alerts_count",
    "get_recent_alerts",
    "init_alerts_db",
    "mark_alerts_seen",
]
