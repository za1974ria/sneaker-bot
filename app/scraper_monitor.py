from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "scraper_monitor.db"


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_scraper_monitor_db() -> None:
    con = _conn()
    try:
        cur = con.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS scraper_health (
                scraper_name TEXT PRIMARY KEY,
                last_run TEXT NOT NULL,
                last_success INTEGER NOT NULL,
                nb_products INTEGER NOT NULL,
                error_message TEXT
            )
            """
        )
        con.commit()
    finally:
        con.close()


def upsert_scraper_health(
    scraper_name: str,
    *,
    last_success: bool,
    nb_products: int,
    error_message: str = "",
) -> None:
    con = _conn()
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO scraper_health(scraper_name,last_run,last_success,nb_products,error_message)
            VALUES(?,?,?,?,?)
            ON CONFLICT(scraper_name) DO UPDATE SET
                last_run=excluded.last_run,
                last_success=excluded.last_success,
                nb_products=excluded.nb_products,
                error_message=excluded.error_message
            """,
            (
                str(scraper_name),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                1 if bool(last_success) else 0,
                max(0, int(nb_products)),
                str(error_message or ""),
            ),
        )
        con.commit()
    finally:
        con.close()


def get_scraper_health_rows() -> list[dict[str, object]]:
    init_scraper_monitor_db()
    con = _conn()
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT scraper_name, last_run, last_success, nb_products, error_message
            FROM scraper_health
            ORDER BY scraper_name COLLATE NOCASE
            """
        )
        rows = cur.fetchall()
    finally:
        con.close()
    out: list[dict[str, object]] = []
    for r in rows:
        out.append(
            {
                "scraper_name": str(r["scraper_name"]),
                "last_run": str(r["last_run"]),
                "last_success": bool(int(r["last_success"] or 0)),
                "nb_products": int(r["nb_products"] or 0),
                "error_message": str(r["error_message"] or ""),
            }
        )
    return out
