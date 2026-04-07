"""
Sitemap XML Scraper — SneakerBot
Extrait des prix depuis les sitemaps (URLs produit) puis pages HTML (JSON-LD / offres).
Cache SQLite 6 h.
"""

from __future__ import annotations

import gzip
import logging
import os
import random
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = _PROJECT_ROOT / "data" / "sitemap_cache.db"

# Garde-fous réseau / mémoire / durée totale d’un scrape complet.
# Certains `sitemap` produits dépassent 15 Mo — surcharge via SITEMAP_MAX_BYTES si besoin.
MAX_SITEMAP_BYTES = int(os.environ.get("SITEMAP_MAX_BYTES", "15000000"))
MAX_HTML_BYTES = int(os.environ.get("SITEMAP_MAX_HTML_BYTES", "8000000"))
MAX_LOCS_PER_DOCUMENT = 8000
MAX_CHILD_SITEMAPS = 35
DEFAULT_SCRAPE_WALL_SEC = 75.0
CONNECT_TIMEOUT_SEC = 12
READ_TIMEOUT_SITEMAP_SEC = 25
READ_TIMEOUT_HTML_SEC = 18

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
    ),
    "Accept": "application/xml,text/xml,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}

HTML_HEADERS = {
    **HEADERS,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

_SESSION_TLS = threading.local()


def _session() -> requests.Session:
    """Session par thread + pool + retries (connexions instables, 429/5xx)."""
    s = getattr(_SESSION_TLS, "session", None)
    if s is None:
        s = requests.Session()
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD"}),
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _SESSION_TLS.session = s
    return s


@contextmanager
def _db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=8000")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_sitemap_db() -> None:
    with _db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sitemap_prices (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                source     TEXT NOT NULL,
                brand      TEXT NOT NULL,
                model      TEXT NOT NULL,
                price      REAL NOT NULL,
                url        TEXT,
                fetched_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_sp_brand_model
            ON sitemap_prices(brand, model)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sitemap_runs (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                source     TEXT NOT NULL,
                nb_prices  INTEGER DEFAULT 0,
                run_at     TEXT NOT NULL,
                status     TEXT DEFAULT 'ok'
            )
            """
        )


def _decode_response_body(url: str, r: requests.Response) -> str | None:
    """Texte XML/HTML ; gère les flux gzip bruts (.gz ou octets gzip)."""
    try:
        data = r.content
        if len(data) > MAX_SITEMAP_BYTES:
            logger.warning("Sitemap trop volumineux ignoré (%s octets): %s", len(data), url[:80])
            return None
        if url.endswith(".gz") or (len(data) >= 2 and data[:2] == b"\x1f\x8b"):
            return gzip.decompress(data).decode("utf-8", errors="replace")
        r.encoding = r.apparent_encoding or "utf-8"
        return r.text
    except Exception as e:  # noqa: BLE001
        logger.debug("decode body %s: %s", url, e)
        return None


def _fetch_sitemap(url: str) -> str | None:
    try:
        r = _session().get(
            url,
            headers=HEADERS,
            timeout=(CONNECT_TIMEOUT_SEC, READ_TIMEOUT_SITEMAP_SEC),
            allow_redirects=True,
        )
        if r.status_code != 200:
            return None
        return _decode_response_body(url, r)
    except Exception as e:  # noqa: BLE001
        logger.debug("Sitemap fetch error %s: %s", url, e)
        return None


def _fetch_html(url: str) -> str | None:
    try:
        r = _session().get(
            url,
            headers=HTML_HEADERS,
            timeout=(CONNECT_TIMEOUT_SEC, READ_TIMEOUT_HTML_SEC),
            allow_redirects=True,
        )
        if r.status_code != 200:
            return None
        if len(r.content) > MAX_HTML_BYTES:
            logger.debug("Page HTML trop grande ignorée: %s", url[:80])
            return None
        r.encoding = r.apparent_encoding or "utf-8"
        return r.text
    except Exception as e:  # noqa: BLE001
        logger.debug("HTML fetch error %s: %s", url, e)
        return None


def _extract_locs(xml: str) -> list[str]:
    found = re.findall(r"<loc>\s*([^<]+?)\s*</loc>", xml, flags=re.I)
    out = [u.strip() for u in found]
    if len(out) > MAX_LOCS_PER_DOCUMENT:
        out = out[:MAX_LOCS_PER_DOCUMENT]
    return out


def _is_sitemap_index(xml: str) -> bool:
    head = xml.lower()[:8000]
    return "<sitemapindex" in head or "sitemapindex" in head


def collect_page_urls_from_sitemap(
    start_url: str,
    depth: int = 2,
    visited: set[str] | None = None,
) -> list[str]:
    """
    Déroule un index de sitemaps puis retourne les URLs de pages (http/https).
    `visited` évite les boucles entre index XML.
    """
    if visited is None:
        visited = set()
    norm = start_url.split("#")[0].strip()
    if norm in visited:
        return []
    visited.add(norm)

    raw = _fetch_sitemap(start_url)
    if not raw:
        return []
    if _is_sitemap_index(raw):
        if depth <= 0:
            return []
        acc: list[str] = []
        for loc in _extract_locs(raw)[:MAX_CHILD_SITEMAPS]:
            if not loc.startswith("http"):
                continue
            if loc.lower().endswith(".xml"):
                acc.extend(collect_page_urls_from_sitemap(loc, depth - 1, visited))
            else:
                acc.append(loc)
        return acc
    return [u for u in _extract_locs(raw) if u.startswith("http")]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _url_relevant(url: str, brand: str, model: str) -> bool:
    """Filtre URLs produit (slug marque + au moins un jeton modèle significatif)."""
    u = unquote(url).lower()
    b = _norm(brand)
    if len(b) >= 3 and b not in re.sub(r"[^a-z0-9]+", "", u):
        bl = (brand or "").lower().replace(" ", "-")
        if bl not in u and (brand or "").lower().replace(" ", "") not in u:
            return False
    toks = [t for t in re.split(r"\W+", (model or "").lower()) if len(t) >= 4]
    weak = {"low", "high", "women", "men", "unisex", "premium", "classic"}
    strong = [t for t in toks if t not in weak][:5]
    if not strong:
        return True
    return any(t in u for t in strong)


def _prices_from_html(html: str) -> list[float]:
    """Prix JSON-LD / offre / microdonnées courants."""
    if not html:
        return []
    out: list[float] = []
    patterns = [
        r'"price"\s*:\s*"?([0-9]+[.,][0-9]{1,2})"?',
        r'"price"\s*:\s*([0-9]+\.[0-9]{2})\b',
        r'itemprop=["\']price["\'][^>]*content=["\']([0-9]+[.,]?[0-9]*)',
        r'property=["\']product:price:amount["\'][^>]*content=["\']([0-9]+[.,]?[0-9]*)',
    ]
    for pat in patterns:
        for m in re.finditer(pat, html, flags=re.I):
            try:
                p = float(m.group(1).replace(",", "."))
                if 30.0 <= p <= 500.0:
                    out.append(round(p, 2))
            except (ValueError, IndexError):
                continue
    return out


def scrape_generic_sitemap(
    source_name: str,
    sitemap_url: str,
    brand: str,
    model: str,
    wall_deadline: float | None = None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    try:
        page_urls = collect_page_urls_from_sitemap(sitemap_url, depth=2)
    except Exception as e:  # noqa: BLE001
        logger.debug("collect_page_urls %s: %s", source_name, e)
        return results

    matched = [u for u in page_urls if _url_relevant(u, brand, model)]
    if not matched:
        matched = [u for u in page_urls if _norm(brand) in _norm(unquote(u))][:8]
    matched = list(dict.fromkeys(matched))[:8]

    for product_url in matched[:5]:
        if wall_deadline is not None and time.monotonic() > wall_deadline:
            break
        time.sleep(random.uniform(0.5, 1.2))
        html = _fetch_html(product_url)
        if not html:
            continue
        found = _prices_from_html(html)
        if not found:
            continue
        price = found[0]
        results.append(
            {
                "brand": brand,
                "model": model,
                "price": price,
                "source": f"{source_name}_sitemap",
                "url": product_url,
                "currency": "EUR",
            }
        )
        break

    return results


# Registre — URLs racine (index ou urlset) ; échecs réseau ignorés silencieusement.
SITEMAP_SOURCES: list[dict[str, Any]] = [
    {"name": "courir.com", "sitemap": "https://www.courir.com/sitemap.xml", "active": True},
    {"name": "sport2000.fr", "sitemap": "https://www.sport2000.fr/sitemap.xml", "active": True},
    {"name": "intersport.fr", "sitemap": "https://www.intersport.fr/sitemap.xml", "active": True},
    {"name": "spartoo.com", "sitemap": "https://www.spartoo.com/sitemap.xml", "active": True},
    {"name": "newbalance.fr", "sitemap": "https://www.newbalance.fr/sitemap.xml", "active": True},
    {
        "name": "asics.com",
        "sitemap": "https://www.asics.com/fr/fr-fr/sitemap.xml",
        "active": True,
    },
    {"name": "puma.com", "sitemap": "https://www.puma.com/sitemap.xml", "active": True},
]


def scrape_all_sitemaps(
    brand: str,
    model: str,
    max_wall_sec: float | None = None,
) -> list[dict[str, Any]]:
    """
    Parcourt les sources actives ; une seule transaction SQLite en fin de passe.
    `max_wall_sec` : arrêt global pour ne pas bloquer le pipeline agrégateur.
    """
    wall = max_wall_sec if max_wall_sec is not None else DEFAULT_SCRAPE_WALL_SEC
    t0 = time.monotonic()
    deadline = t0 + max(5.0, wall)

    all_results: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc).isoformat()

    for source in SITEMAP_SOURCES:
        if time.monotonic() > deadline:
            logger.debug("Sitemap scrape: budget temps atteint pour %s %s", brand, model)
            break
        if not source.get("active"):
            continue
        name = str(source.get("name") or "unknown")
        surl = str(source.get("sitemap") or "")
        if not surl:
            continue
        n_inserted = 0
        try:
            batch = scrape_generic_sitemap(
                name,
                surl,
                brand,
                model,
                wall_deadline=deadline,
            )
            if batch:
                all_results.extend(batch)
                n_inserted = len(batch)
                logger.info(
                    "Sitemap %s: %d prix pour %s %s",
                    name,
                    len(batch),
                    brand,
                    model,
                )
            with _db() as conn:
                conn.execute(
                    """
                    INSERT INTO sitemap_runs (source, nb_prices, run_at, status)
                    VALUES (?, ?, ?, ?)
                    """,
                    (name, n_inserted, now, "ok"),
                )
        except Exception as e:  # noqa: BLE001
            logger.debug("Sitemap %s error: %s", name, e)
            with _db() as conn:
                conn.execute(
                    """
                    INSERT INTO sitemap_runs (source, nb_prices, run_at, status)
                    VALUES (?, ?, ?, ?)
                    """,
                    (name, 0, now, f"err:{str(e)[:80]}"),
                )

        time.sleep(random.uniform(1.0, 2.0))

        if len(all_results) >= 4:
            break

    if all_results:
        try:
            with _db() as conn:
                conn.execute(
                    "DELETE FROM sitemap_prices WHERE brand=? AND model=?",
                    (brand, model),
                )
                for r in all_results:
                    conn.execute(
                        """
                        INSERT INTO sitemap_prices
                        (source, brand, model, price, url, fetched_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            r["source"],
                            brand,
                            model,
                            float(r["price"]),
                            r.get("url") or "",
                            now,
                        ),
                    )
        except Exception as e:  # noqa: BLE001
            logger.warning("Sitemap persistance SQLite échouée: %s", e)

    return all_results


def get_sitemap_prices(
    brand: str,
    model: str,
    max_age_hours: int = 6,
    max_wall_sec: float | None = None,
) -> list[float]:
    """Retourne les prix en cache (< 6 h) ou relance un scrape (borné par `max_wall_sec`)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT price FROM sitemap_prices
            WHERE brand=? AND model=? AND fetched_at > ?
            ORDER BY price
            """,
            (brand, model, cutoff),
        ).fetchall()
    if rows:
        return [float(r["price"]) for r in rows]

    wall = max_wall_sec if max_wall_sec is not None else DEFAULT_SCRAPE_WALL_SEC
    results = scrape_all_sitemaps(brand, model, max_wall_sec=wall)
    return [float(r["price"]) for r in results]
