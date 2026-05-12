"""Lightweight visitors analytics (SQLite)."""

from __future__ import annotations

import csv
import io
import ipaddress
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "visitors.db"
_COUNTRY_CACHE: dict[str, tuple[str, float]] = {}
_COUNTRY_CACHE_TTL_SEC = 24 * 3600
_NOISE_PATH_PREFIXES = (
    "/api/admin/visitors/",
    "/api/update/",
)
_NOISE_PATH_EXACT = {
    "/favicon.ico",
    "/health",
}


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_visitors_db() -> None:
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS visits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                visited_at TEXT NOT NULL,
                ip TEXT NOT NULL,
                country TEXT,
                path TEXT NOT NULL,
                user_agent TEXT,
                status_code INTEGER
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_visits_at ON visits(visited_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_visits_path ON visits(path)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_visits_ip ON visits(ip)")


def track_visit(*, ip: str, path: str, user_agent: str, country: str, status_code: int) -> None:
    p = (path or "").strip()
    if p in _NOISE_PATH_EXACT:
        return
    for pref in _NOISE_PATH_PREFIXES:
        if p.startswith(pref):
            return
    resolved_country = resolve_country(ip=ip, country_hint=country)
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO visits(visited_at, ip, country, path, user_agent, status_code)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (now, ip[:80], resolved_country[:32], path[:512], user_agent[:512], int(status_code)),
        )
        # keep DB small (rolling ~14 days)
        cutoff = (datetime.utcnow() - timedelta(days=14)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("DELETE FROM visits WHERE visited_at < ?", (cutoff,))


def _country_from_astroscan_geoip(ip: str) -> str:
    """
    Utilise AstroScan geoip si le module est présent.
    Aucun crash si indisponible.
    """
    try:
        import astroscan_geoip  # type: ignore

        if hasattr(astroscan_geoip, "lookup_country"):
            val = astroscan_geoip.lookup_country(ip)  # type: ignore[attr-defined]
            return str(val or "").strip()
        if hasattr(astroscan_geoip, "get_country"):
            val = astroscan_geoip.get_country(ip)  # type: ignore[attr-defined]
            return str(val or "").strip()
    except Exception:
        pass
    return ""


def _country_from_ip_api(ip: str) -> str:
    try:
        r = requests.get(f"http://ip-api.com/json/{ip}", timeout=1.0)
        if r.status_code != 200:
            return ""
        data = r.json() if r is not None else {}
        if str(data.get("status") or "").lower() != "success":
            return ""
        return str(data.get("country") or "").strip()
    except Exception:
        return ""


def resolve_country(*, ip: str, country_hint: str = "") -> str:
    hint = (country_hint or "").strip()
    if hint and hint.lower() not in {"unknown", "n/a", "inconnu"}:
        return hint
    ip_raw = (ip or "").strip()
    if not ip_raw or ip_raw.lower() == "unknown":
        return "Inconnu"
    if ip_raw in {"127.0.0.1", "::1", "localhost"}:
        return "Serveur"
    now_ts = datetime.utcnow().timestamp()
    cached = _COUNTRY_CACHE.get(ip_raw)
    if cached and (now_ts - float(cached[1])) < _COUNTRY_CACHE_TTL_SEC:
        return cached[0]
    try:
        ip_obj = ipaddress.ip_address(ip_raw)
        if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local:
            _COUNTRY_CACHE[ip_raw] = ("Serveur", now_ts)
            return "Serveur"
    except Exception:
        pass
    country = _country_from_astroscan_geoip(ip_raw)
    if not country:
        country = _country_from_ip_api(ip_raw)
    if not country:
        country = "Inconnu"
    _COUNTRY_CACHE[ip_raw] = (country, now_ts)
    return country


def get_visitors_summary() -> dict[str, Any]:
    now = datetime.utcnow()
    day_start = now.strftime("%Y-%m-%d 00:00:00")
    d24 = (now - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    d7 = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")

    with _conn() as conn:
        today_unique = conn.execute(
            "SELECT COUNT(DISTINCT ip) AS c FROM visits WHERE visited_at >= ?",
            (day_start,),
        ).fetchone()
        today_total = conn.execute(
            "SELECT COUNT(*) AS c FROM visits WHERE visited_at >= ?",
            (day_start,),
        ).fetchone()
        today_pages = conn.execute(
            "SELECT COUNT(*) AS c FROM visits WHERE visited_at >= ?",
            (day_start,),
        ).fetchone()
        last_visits = conn.execute(
            """
            SELECT visited_at, ip, country, path, user_agent
            FROM visits
            ORDER BY id DESC
            LIMIT 20
            """
        ).fetchall()
        top_pages = conn.execute(
            """
            SELECT path, COUNT(*) AS c
            FROM visits
            WHERE visited_at >= ?
            GROUP BY path
            ORDER BY c DESC
            LIMIT 10
            """,
            (d24,),
        ).fetchall()
        top_countries = conn.execute(
            """
            SELECT
                COALESCE(NULLIF(country, ''), 'Inconnu') AS country,
                COUNT(*) AS total_visits,
                COUNT(DISTINCT ip) AS unique_visitors
            FROM visits
            WHERE visited_at >= ?
              AND path <> '/favicon.ico'
              AND path NOT LIKE '/api/admin/visitors/%'
              AND path NOT LIKE '/api/update/%'
              AND path <> '/health'
            GROUP BY COALESCE(NULLIF(country, ''), 'Inconnu')
            ORDER BY total_visits DESC
            LIMIT 10
            """,
            (d7,),
        ).fetchall()

        has_real_country = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM visits
            WHERE visited_at >= ?
              AND path <> '/favicon.ico'
              AND path NOT LIKE '/api/admin/visitors/%'
              AND path NOT LIKE '/api/update/%'
              AND path <> '/health'
              AND country IS NOT NULL
              AND TRIM(country) <> ''
              AND TRIM(country) <> 'Inconnu'
            """,
            (d7,),
        ).fetchone()

    return {
        "today": {
            "unique_visitors": int((today_unique or {"c": 0})["c"]),
            "total_visits": int((today_total or {"c": 0})["c"]),
            "page_views": int((today_pages or {"c": 0})["c"]),
        },
        "latest_visits": [
            {
                "time": str(r["visited_at"] or ""),
                "ip": str(r["ip"] or ""),
                "country": str(r["country"] or ""),
                "path": str(r["path"] or ""),
                "user_agent": str(r["user_agent"] or "")[:80],
            }
            for r in last_visits
        ],
        "top_pages_24h": [{"path": str(r["path"]), "count": int(r["c"])} for r in top_pages],
        "top_countries_7d": [
            {
                "country": str(r["country"]),
                "count": int(r["total_visits"]),
                "unique_visitors": int(r["unique_visitors"]),
            }
            for r in top_countries
            if not (int((has_real_country or {"c": 0})["c"]) > 0 and str(r["country"]) == "Inconnu")
        ],
    }


def export_visits_csv(limit: int = 5000) -> str:
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT visited_at, ip, country, path, user_agent, status_code
            FROM visits
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(1, min(limit, 20000)),),
        ).fetchall()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["visited_at", "ip", "country", "path", "user_agent", "status_code"])
    for r in rows:
        w.writerow(
            [
                str(r["visited_at"] or ""),
                str(r["ip"] or ""),
                str(r["country"] or ""),
                str(r["path"] or ""),
                str(r["user_agent"] or ""),
                str(r["status_code"] or ""),
            ]
        )
    return out.getvalue()


def backfill_unknown_countries(limit: int = 50) -> int:
    """
    Corrige les dernières IP sans pays (Inconnu/NULL/vide), max `limit`.
    """
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT id, ip, country
            FROM visits
            WHERE COALESCE(NULLIF(country, ''), 'Inconnu') = 'Inconnu'
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(1, min(limit, 500)),),
        ).fetchall()
        updated = 0
        for r in rows:
            rid = int(r["id"])
            ip = str(r["ip"] or "").strip()
            resolved = resolve_country(ip=ip, country_hint=str(r["country"] or ""))
            if not resolved:
                resolved = "Inconnu"
            conn.execute("UPDATE visits SET country=? WHERE id=?", (resolved[:32], rid))
            updated += 1
        return updated

