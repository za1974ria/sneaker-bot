"""FastAPI entrypoint for Sneakers Market Intelligence."""

from __future__ import annotations

import bcrypt
import json
import logging
import os
import secrets
import smtplib
import string
import threading
import time
import csv
from collections import defaultdict, deque
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.analytics.tracker import get_stats, log_event
from app.scraper_monitor import get_scraper_health_rows, init_scraper_monitor_db
from app.subscription_manager import (
    build_whatsapp_admin_wa_me_url,
    build_whatsapp_client_login_wa_me_url,
    get_all_subscriptions,
    get_admin_whatsapp_digits,
    get_pending_subscriptions,
    get_unsent_admin_notifications,
    public_base_url,
    reject_subscription,
    submit_subscription,
    validate_subscription,
)
from app.ai_supervisor import AISupervisor

from app.services.market_service import (
    get_arbitrage_opportunities,
    get_sneaker_catalog,
    get_market_products,
    get_market_snapshot,
)

_scheduler_started = False
# Entrypoint de production officiel: `uvicorn app.app:app` (service systemd sneaker_bot.service).
# Les autres points d'entrée du repo sont conservés pour compatibilité/dev.
PROD_ENTRYPOINT = "app.app:app"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Démarre le scheduler de scraping une seule fois par processus.
    En cas d'échec, l'erreur est loggée sans faire tomber l'application.
    """
    global _scheduler_started
    if not _scheduler_started:
        try:
            from scheduler import start_scheduler

            start_scheduler()
        except Exception:
            logging.getLogger(__name__).exception(
                "Échec du démarrage du scheduler de scraping (APScheduler)"
            )
        else:
            _scheduler_started = True
    try:
        from app.google_shopping_verifier import init_google_cache

        init_google_cache()
    except Exception:
        logging.getLogger(__name__).exception("Échec init_google_cache (SQLite)")
    try:
        init_scraper_monitor_db()
    except Exception:
        logging.getLogger(__name__).exception("Échec init_scraper_monitor_db (SQLite)")
    try:
        from scrapers.sitemap_scraper import init_sitemap_db

        init_sitemap_db()
    except Exception:
        logging.getLogger(__name__).exception("Échec init_sitemap_db (SQLite)")
    _log_critical_env_status()
    yield


app = FastAPI(title="Sneaker Bot", lifespan=lifespan)

logger = logging.getLogger(__name__)

AUTH_USERNAME = (os.getenv("APP_LOGIN_USER") or "admin").strip()
AUTH_PASSWORD = (os.getenv("APP_LOGIN_PASSWORD") or "admin123").strip()
AUTH_TOKEN = (os.getenv("APP_AUTH_TOKEN") or "sneakerbot-auth-token").strip()
LOGIN_WINDOW_SEC = 10 * 60
LOGIN_MAX_ATTEMPTS = 8
LOGIN_BLOCK_SEC = 15 * 60
_login_failures: dict[str, deque[float]] = defaultdict(deque)
_login_block_until: dict[str, float] = {}
RATE_WINDOW_SEC = 60
RATE_MAX_REQUESTS_PER_WINDOW = 1200
UNAUTH_WINDOW_SEC = 300
UNAUTH_MAX_PER_WINDOW = 120
IP_BAN_SEC = 24 * 60 * 60
_security_lock = threading.Lock()
_req_hits_by_ip: dict[str, deque[float]] = defaultdict(deque)
_unauth_hits_by_ip: dict[str, deque[float]] = defaultdict(deque)
_banned_ips: dict[str, float] = {}
TRUSTED_IPS = {"127.0.0.1", "::1", "localhost"}
API_LIVE_GOOGLE_VERIFY_ENABLED = (os.getenv("API_LIVE_GOOGLE_VERIFY_ENABLED") or "0").strip() == "1"
DATA_FRESHNESS_CACHE_TTL_SEC = 30
_data_freshness_cache: dict[str, object] = {"ts": 0.0, "payload": None}


def _log_critical_env_status() -> None:
    base_url = public_base_url()
    required = (
        "PUBLIC_BASE_URL",
        "APP_AUTH_TOKEN",
        "GROQ_API_KEY",
        "ANTHROPIC_API_KEY",
        "SERPAPI_KEY",
        "ADMIN_WHATSAPP_NUMBER",
    )
    missing = [name for name in required if not (os.getenv(name) or "").strip()]
    if missing:
        logger.warning("Variables critiques manquantes: %s", ", ".join(missing))
    if not (os.getenv("ADMIN_WHATSAPP_NUMBER") or "").strip() and (os.getenv("ADMIN_WHATSAPP") or "").strip():
        logger.info("ADMIN_WHATSAPP_NUMBER absent, fallback actif via ADMIN_WHATSAPP")
    if not base_url.startswith(("http://", "https://")):
        logger.warning("PUBLIC_BASE_URL invalide (attendu http/https): %s", base_url)
    if AUTH_TOKEN == "sneakerbot-auth-token":
        logger.warning("APP_AUTH_TOKEN utilise la valeur par défaut (à remplacer en production)")

MARKET_FR_SOURCES_CSV = Path(__file__).resolve().parent.parent / "data" / "market_fr_sources.csv"
MARKET_FR_CSV = Path(__file__).resolve().parent.parent / "data" / "market_fr.csv"
FR_SOURCES_CATALOG_PATH = Path(__file__).resolve().parent.parent / "data" / "fr_ecommerce_sources.json"
FR_UPDATE_STATUS_PATH = Path(__file__).resolve().parent.parent / "data" / "fr_update_status.json"
SECURITY_STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "security_state.json"
ACCESS_CONTROL_PATH = Path(__file__).resolve().parent.parent / "data" / "access_control.json"
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _fr_last_update_meta() -> tuple[str, str]:
    candidates = [MARKET_FR_SOURCES_CSV, MARKET_FR_CSV]
    mtimes: list[float] = []
    for p in candidates:
        if p.is_file():
            try:
                mtimes.append(p.stat().st_mtime)
            except OSError:
                continue
    if not mtimes:
        return "--/-- --:--", "stale"
    last_dt = datetime.fromtimestamp(max(mtimes))
    age_seconds = max(0.0, time.time() - last_dt.timestamp())
    age_hours = age_seconds / 3600.0
    if age_hours <= 1.0:
        freshness = "fresh"
    elif age_hours <= 3.0:
        freshness = "aging"
    else:
        freshness = "stale"
    return last_dt.strftime("%d/%m %H:%M"), freshness


def _fr_clock_health() -> tuple[str, str]:
    """
    Santé de l'horloge de mise à jour:
    - ok: < 70 min
    - warning: 70 à 120 min
    - critical: > 120 min ou inconnu
    """
    candidates = [MARKET_FR_SOURCES_CSV, MARKET_FR_CSV]
    mtimes: list[float] = []
    for p in candidates:
        if p.is_file():
            try:
                mtimes.append(p.stat().st_mtime)
            except OSError:
                continue
    if not mtimes:
        return "critical", "horloge inconnue"
    age_minutes = (time.time() - max(mtimes)) / 60.0
    if age_minutes < 70:
        return "ok", "OK horloge"
    if age_minutes <= 120:
        return "warning", "retard horloge"
    return "critical", "alerte horloge"


def _safe_csv_row_count(path: Path) -> int:
    if not path.is_file():
        return 0
    try:
        with path.open(encoding="utf-8", newline="") as f:
            return sum(1 for _ in csv.DictReader(f))
    except Exception:
        logger.warning("Lecture impossible pour comptage lignes: %s", path.name)
        return 0


def _safe_distinct_shops_count(path: Path) -> int:
    if not path.is_file():
        return 0
    try:
        with path.open(encoding="utf-8", newline="") as f:
            shops = {str(r.get("shop") or "").strip() for r in csv.DictReader(f)}
        return len({s for s in shops if s})
    except Exception:
        logger.warning("Lecture impossible pour comptage shops: %s", path.name)
        return 0


def _data_freshness_snapshot() -> dict[str, object]:
    now_ts = time.time()
    cached_ts = float(_data_freshness_cache.get("ts") or 0.0)
    cached_payload = _data_freshness_cache.get("payload")
    if cached_payload is not None and (now_ts - cached_ts) <= DATA_FRESHNESS_CACHE_TTL_SEC:
        return dict(cached_payload)  # shallow copy for safety

    def _meta(path: Path, stale_after_minutes: float) -> dict[str, object]:
        exists = path.is_file()
        mtime = 0.0
        if exists:
            try:
                mtime = float(path.stat().st_mtime)
            except OSError:
                mtime = 0.0
        age_minutes = round((now_ts - mtime) / 60.0, 2) if mtime > 0 else None
        stale = bool(age_minutes is None or age_minutes > stale_after_minutes)
        return {
            "exists": exists,
            "updated_at": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S") if mtime > 0 else "",
            "age_minutes": age_minutes,
            "stale": stale,
        }

    market = _meta(MARKET_FR_CSV, stale_after_minutes=360.0)
    market["rows"] = _safe_csv_row_count(MARKET_FR_CSV)
    sources = _meta(MARKET_FR_SOURCES_CSV, stale_after_minutes=180.0)
    sources["rows"] = _safe_csv_row_count(MARKET_FR_SOURCES_CSV)
    sources["distinct_shops"] = _safe_distinct_shops_count(MARKET_FR_SOURCES_CSV)

    warnings: list[str] = []
    if not market["exists"] or int(market.get("rows") or 0) <= 0:
        warnings.append("market_fr_csv_vide_ou_absent")
    if not sources["exists"] or int(sources.get("rows") or 0) <= 0:
        warnings.append("market_fr_sources_csv_vide_ou_absent")
    if int(sources.get("distinct_shops") or 0) <= 0:
        warnings.append("aucune_boutique_distincte")
    if bool(market.get("stale")):
        warnings.append("market_fr_csv_perime")
    if bool(sources.get("stale")):
        warnings.append("market_fr_sources_csv_perime")
    if warnings:
        logger.warning("Alerte fraîcheur data FR: %s", ", ".join(warnings))

    payload = {"market_fr_csv": market, "market_fr_sources_csv": sources, "warnings": warnings}
    _data_freshness_cache["ts"] = now_ts
    _data_freshness_cache["payload"] = payload
    return dict(payload)


def _load_security_state() -> None:
    if not SECURITY_STATE_PATH.is_file():
        return
    try:
        raw = json.loads(SECURITY_STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return
        banned = raw.get("banned_ips") or {}
        if not isinstance(banned, dict):
            return
        now_ts = time.time()
        with _security_lock:
            for ip, until in banned.items():
                try:
                    ts = float(until)
                except (TypeError, ValueError):
                    continue
                if ts > now_ts:
                    _banned_ips[str(ip)] = ts
    except Exception:
        logging.getLogger(__name__).exception("Impossible de charger security_state.json")


def _save_security_state() -> None:
    try:
        now_ts = time.time()
        with _security_lock:
            clean = {ip: until for ip, until in _banned_ips.items() if float(until) > now_ts}
        SECURITY_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        SECURITY_STATE_PATH.write_text(
            json.dumps({"banned_ips": clean, "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        logging.getLogger(__name__).exception("Impossible d'ecrire security_state.json")


def _client_ip(request: Request) -> str:
    x_real_ip = str(request.headers.get("x-real-ip") or "").strip()
    if x_real_ip:
        return x_real_ip
    xff = str(request.headers.get("x-forwarded-for") or "").strip()
    if xff:
        return xff.split(",")[0].strip()
    if request.client and request.client.host:
        return str(request.client.host)
    return "unknown"


def _request_is_https(request: Request) -> bool:
    """Schéma réel du client derrière Nginx (X-Forwarded-Proto) ou connexion directe."""
    if request.url.scheme == "https":
        return True
    return str(request.headers.get("x-forwarded-proto", "")).lower() == "https"


def _is_ip_banned(ip: str) -> bool:
    if ip in TRUSTED_IPS:
        return False
    now_ts = time.time()
    with _security_lock:
        until = float(_banned_ips.get(ip) or 0.0)
        if until <= now_ts:
            if ip in _banned_ips:
                _banned_ips.pop(ip, None)
                _save_security_state()
            return False
        return True


def _ban_ip(ip: str, reason: str) -> None:
    if ip in TRUSTED_IPS:
        return
    if not ip or ip == "unknown":
        return
    with _security_lock:
        _banned_ips[ip] = time.time() + IP_BAN_SEC
    logging.getLogger(__name__).warning("IP bannie: %s reason=%s", ip, reason)
    _save_security_state()


def _record_hit_and_detect_flood(ip: str) -> bool:
    if ip in TRUSTED_IPS:
        return False
    now_ts = time.time()
    with _security_lock:
        q = _req_hits_by_ip[ip]
        while q and (now_ts - q[0]) > RATE_WINDOW_SEC:
            q.popleft()
        q.append(now_ts)
        return len(q) > RATE_MAX_REQUESTS_PER_WINDOW


def _record_unauth_and_detect(ip: str) -> bool:
    if ip in TRUSTED_IPS:
        return False
    now_ts = time.time()
    with _security_lock:
        q = _unauth_hits_by_ip[ip]
        while q and (now_ts - q[0]) > UNAUTH_WINDOW_SEC:
            q.popleft()
        q.append(now_ts)
        return len(q) > UNAUTH_MAX_PER_WINDOW


def _is_authenticated(request: Request) -> bool:
    cookie = str(request.cookies.get("sb_auth") or "").strip()
    return bool(cookie and cookie == AUTH_TOKEN)


def _session_role(request: Request) -> str:
    return str(request.cookies.get("sb_role") or "").strip().lower()


def _is_admin_session(request: Request) -> bool:
    return _is_authenticated(request) and _session_role(request) == "admin"


def _is_client_session(request: Request) -> bool:
    return _is_authenticated(request) and _session_role(request) == "client"


def _hash_password(plain: str) -> str:
    """Retourne le hash bcrypt du mot de passe en clair."""
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_password(plain: str, stored: str) -> bool:
    """Vérifie un mot de passe contre un hash bcrypt (ou plaintext legacy)."""
    if not plain or not stored:
        return False
    if stored.startswith("$2b$") or stored.startswith("$2a$"):
        try:
            return bcrypt.checkpw(plain.encode("utf-8"), stored.encode("utf-8"))
        except Exception:
            return False
    # Fallback plaintext pour migration (supprimé une fois tous les hashes migrés)
    return plain.strip() == stored.strip()


def _load_access_control() -> dict[str, object]:
    default_payload: dict[str, object] = {
        "sales_mode": "open",
        "users": [
            {
                "username": AUTH_USERNAME,
                "password": AUTH_PASSWORD,
                "role": "admin",
                "active": True,
            }
        ],
    }
    if not ACCESS_CONTROL_PATH.is_file():
        return default_payload
    try:
        raw = json.loads(ACCESS_CONTROL_PATH.read_text(encoding="utf-8"))
    except Exception:
        logging.getLogger(__name__).exception("Lecture impossible: %s", ACCESS_CONTROL_PATH)
        return default_payload
    if not isinstance(raw, dict):
        return default_payload
    sales_mode = str(raw.get("sales_mode") or "open").strip().lower()
    users_raw = raw.get("users")
    users: list[dict[str, object]] = []
    if isinstance(users_raw, list):
        for u in users_raw:
            if not isinstance(u, dict):
                continue
            username = str(u.get("username") or "").strip()
            # Ne pas strip le mot de passe : doit matcher exactement le JSON / ce que le client copie.
            password = str(u.get("password") or "")
            role = str(u.get("role") or "client").strip().lower()
            active = bool(u.get("active", True))
            if not username or password == "":
                continue
            merged: dict[str, object] = dict(u)
            merged["username"] = username
            merged["password"] = password
            merged["role"] = role if role in {"admin", "client"} else "client"
            merged["active"] = active
            users.append(merged)
    if not users:
        users = list(default_payload["users"])  # type: ignore[arg-type]
    return {"sales_mode": sales_mode if sales_mode in {"open", "closed"} else "open", "users": users}


def _find_user(username: str, password: str) -> dict[str, object] | None:
    """
    Authentifie contre data/access_control.json : clé « users », puis « accounts »,
    puis identifiants à la racine (ancien schéma admin).
    """
    uname = str(username or "").strip()
    pw = str(password or "").strip()
    if not uname or not pw:
        return None

    raw: dict[str, object] = {}
    if ACCESS_CONTROL_PATH.is_file():
        try:
            loaded = json.loads(ACCESS_CONTROL_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
        except Exception:
            logging.getLogger(__name__).exception(
                "Lecture access_control pour _find_user: %s", ACCESS_CONTROL_PATH
            )
            return None

    def _match_entry(entry: object) -> dict[str, object] | None:
        if not isinstance(entry, dict):
            return None
        if not bool(entry.get("active", True)):
            return None
        u_name = str(entry.get("username") or "").strip()
        u_pw = str(entry.get("password") or "")
        if u_name == uname and _verify_password(pw, u_pw):
            return dict(entry)
        return None

    users_list = raw.get("users")
    if isinstance(users_list, list):
        for user in users_list:
            m = _match_entry(user)
            if m is not None:
                return m

    accounts_list = raw.get("accounts")
    if isinstance(accounts_list, list):
        for user in accounts_list:
            m = _match_entry(user)
            if m is not None:
                return m

    root_user = raw.get("username")
    root_pass = raw.get("password")
    if root_user is not None and root_pass is not None:
        ru = str(root_user).strip()
        rp = str(root_pass)
        if ru == uname and _verify_password(pw, rp):
            role = str(raw.get("role") or "admin").strip().lower()
            if role not in {"admin", "client"}:
                role = "admin"
            return {
                "username": ru,
                "password": rp,
                "role": role,
                "active": True,
            }

    return None


def _register_user(username: str, password: str, *, role: str = "client") -> tuple[bool, str]:
    uname = "".join(str(username or "").split()).strip()
    pw = str(password or "")
    if len(uname) < 4:
        return False, "Identifiant trop court (min 4 caractères)."
    if len(pw) < 6:
        return False, "Mot de passe trop court (min 6 caractères)."

    payload = _load_access_control()
    users_raw = payload.get("users")
    users: list[dict[str, object]] = list(users_raw) if isinstance(users_raw, list) else []
    for u in users:
        if not isinstance(u, dict):
            continue
        if str(u.get("username") or "").strip().lower() == uname.lower():
            return False, "Identifiant déjà utilisé."

    users.append(
        {
            "username": uname,
            "password": _hash_password(pw),
            "role": "client" if role != "admin" else "admin",
            "active": True,
            "created_at": datetime.now().isoformat(),
        }
    )
    out = {"sales_mode": payload.get("sales_mode") or "open", "users": users}
    try:
        ACCESS_CONTROL_PATH.parent.mkdir(parents=True, exist_ok=True)
        ACCESS_CONTROL_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        logging.getLogger(__name__).exception("Ecriture access_control impossible")
        return False, "Erreur serveur lors de l'inscription."
    return True, ""


_load_security_state()


def _require_auth_or_raise(request: Request) -> None:
    if _is_authenticated(request):
        return
    raise HTTPException(status_code=401, detail="Authentification requise")


# Routes accessibles sans cookie (GET + POST même path si applicable, ex. /subscribe).
PUBLIC_PATHS_EXACT = frozenset(
    {
        "/",
        "/subscribe",
        "/login",
        "/register",
        "/mentions-legales",
        "/ping",
        "/health",
        "/favicon.ico",
        "/openapi.json",
        "/docs",
        "/redoc",
        "/robots.txt",
    }
)
PUBLIC_PATH_PREFIXES = (
    "/docs/",
    "/redoc/",
    "/api/public/",
    "/api/subscription/",
)


def _is_public_path(path: str) -> bool:
    if path in PUBLIC_PATHS_EXACT:
        return True
    for prefix in PUBLIC_PATH_PREFIXES:
        if path.startswith(prefix):
            return True
    return False


@app.middleware("http")
async def auth_gate_middleware(request: Request, call_next):
    try:
        ip = _client_ip(request)
        if _is_ip_banned(ip):
            return JSONResponse({"detail": "IP bannie temporairement"}, status_code=403)

        path = request.url.path or "/"
        # Limiter la détection flood aux endpoints les plus sensibles.
        if path.startswith("/api/") or (path == "/login" and request.method.upper() == "POST"):
            if _record_hit_and_detect_flood(ip):
                _ban_ip(ip, "flood_rate_limit")
                return JSONResponse({"detail": "IP bannie pour trafic suspect"}, status_code=403)
        if _is_public_path(path):
            response = await call_next(request)
        elif (
            str((_load_access_control().get("sales_mode") or "open")).lower() == "closed"
            and not _is_admin_session(request)
            and not _is_client_session(request)
        ):
            if path.startswith("/api/"):
                response = JSONResponse({"detail": "Acces commercial temporairement ferme"}, status_code=403)
            else:
                response = RedirectResponse(url="/login?next=/comparison", status_code=307)
        elif _is_authenticated(request):
            response = await call_next(request)
        elif path.startswith("/api/"):
            if _record_unauth_and_detect(ip):
                return JSONResponse({"detail": "Trop de requetes non authentifiees"}, status_code=429)
            response = JSONResponse({"detail": "Authentification requise"}, status_code=401)
        else:
            next_q = quote(path, safe="/?=&")
            response = RedirectResponse(url=f"/login?next={next_q}", status_code=307)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).exception("Middleware error: %s", exc)
        response = JSONResponse({"detail": "Erreur serveur temporaire"}, status_code=500)

    # En-têtes sécurité minimales côté navigateur.
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    return response


# Validation "irréprochable" : précision prioritaire
STRICT_MIN_SAMPLE_COUNT = 2
STRICT_MAX_SPREAD_RATIO = 1.75
STRICT_MIN_SCORE = 74
STRICT_MAX_MIDPOINT_DEVIATION_RATIO = 0.28


BRAND_VALIDATION_RULES: dict[str, dict[str, float]] = {
    # max_spread_ratio: plafond min/max ; max_midpoint_dev: recentrage avg
    "Nike": {"max_spread_ratio": 1.75, "max_midpoint_dev": 0.27},
    "Adidas": {"max_spread_ratio": 1.75, "max_midpoint_dev": 0.27},
    "New Balance": {"max_spread_ratio": 1.8, "max_midpoint_dev": 0.28},
    "Salomon": {"max_spread_ratio": 1.7, "max_midpoint_dev": 0.25},
    "Asics": {"max_spread_ratio": 1.75, "max_midpoint_dev": 0.27},
    "Puma": {"max_spread_ratio": 1.8, "max_midpoint_dev": 0.28},
    "Reebok": {"max_spread_ratio": 1.8, "max_midpoint_dev": 0.28},
    "Vans": {"max_spread_ratio": 1.75, "max_midpoint_dev": 0.27},
    "Converse": {"max_spread_ratio": 1.75, "max_midpoint_dev": 0.27},
    "On Running": {"max_spread_ratio": 1.65, "max_midpoint_dev": 0.24},
}


def _public_shop_alias(shop_name: str) -> str:
    """
    Camouflage des boutiques côté exposition publique.
    """
    s = _normalize_key(shop_name)
    if s in {"courir"}:
        return "Retailer A"
    if s in {"foot locker"}:
        return "Retailer B"
    if s in {"snipes"}:
        return "Retailer C"
    if s in {"sports direct"}:
        return "Retailer D"
    if s in {"agrege fr", "agrege_fr", "agregé fr"}:
        return "Source Agrégée"
    if s in {"manual fr", "manuel fr"}:
        return "Source Manuelle"
    return "Retailer X"


def _comparison_position_client(price_avgs: list[float], our_avg: float) -> str:
    """Position du prix moyen boutique vs toutes les moyennes du même modèle (marché FR)."""
    vals = sorted([float(x) for x in price_avgs if float(x) > 0])
    n = len(vals)
    if n <= 1:
        return "Milieu de gamme"
    rank: int | None = None
    for i, v in enumerate(vals):
        if abs(v - our_avg) < 1e-6:
            rank = i
            break
    if rank is None:
        import bisect

        rank = bisect.bisect_left(vals, our_avg)
        rank = min(rank, n - 1)
    frac = rank / (n - 1)
    if frac <= 0.10:
        return "Top 10%"
    if frac <= 0.25:
        return "Top 25%"
    if frac >= 0.75:
        return "Haut de gamme"
    return "Milieu de gamme"


def _comparison_recommendation(
    item: dict[str, object],
    min_avg_model: float | None,
) -> str:
    score = int(item.get("score") or 0)
    gb = str(item.get("google_badge") or "none")
    pavg = float(item.get("price_avg") or 0.0)
    g_price = item.get("google_price")
    raw_signed = item.get("google_deviation_signed_pct")
    signed_f: float | None
    if raw_signed is None:
        signed_f = None
    else:
        try:
            signed_f = float(raw_signed)
        except (TypeError, ValueError):
            signed_f = None

    if score >= 90 and gb == "ok":
        return "⭐ MEILLEUR PRIX"
    if min_avg_model is not None and pavg > 0 and abs(pavg - min_avg_model) < 1e-6:
        return "💰 PRIX LE PLUS BAS"
    if signed_f is not None and -5.0 <= signed_f <= 5.0:
        return "✅ PRIX MARCHÉ"
    try:
        gp = float(g_price) if g_price is not None else 0.0
    except (TypeError, ValueError):
        gp = 0.0
    if gp > 0 and pavg > gp * 1.15:
        return "⚠️ PRIX ÉLEVÉ"
    return "👍 BON PRIX"


_supervisor_lock = threading.Lock()
_supervisor_thread: threading.Thread | None = None
_supervisor_results: dict[str, object] = {
    "job_id": "",
    "status": "idle",
    "progress": "0/0",
    "done": 0,
    "total": 0,
    "results": [],
}


def _project_root() -> Path:
    """Répertoire sneaker_bot (parent du package app)."""
    return Path(__file__).resolve().parent.parent


def _load_models_catalog_from_json() -> dict[str, list[str]]:
    """
    Charge data/models_list.json à chaque appel.
    Retourne { marque: [modèles...] } (ordre du fichier, sans doublons).
    """
    path = _project_root() / "data" / "models_list.json"
    if not path.is_file():
        logging.getLogger(__name__).warning("Fichier manquant: %s", path)
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logging.getLogger(__name__).exception("Lecture impossible: %s", path)
        return {}
    if not isinstance(raw, list):
        return {}
    out: dict[str, list[str]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        b = str(item.get("brand") or "").strip()
        m = str(item.get("model") or "").strip()
        if not b or not m:
            continue
        if b not in out:
            out[b] = []
        if m not in out[b]:
            out[b].append(m)
    return out


def _build_catalog_families(catalog: dict[str, list[str]]) -> dict[str, dict[str, list[str]]]:
    """
    Construit une vue familles -> types pour chaque marque.
    Heuristique: retire les suffixes de variante (Low/High/OG/GTX/...).
    """
    variant_tokens = {
        "low", "high", "mid", "og", "premium", "essential", "vintage", "retro",
        "advanced", "gore-tex", "gtx", "waterproof", "platform", "pro", "plus",
        "metallic", "indoor", "bold", "reissue", "tapered", "stacked", "form",
    }
    out: dict[str, dict[str, list[str]]] = {}
    for brand, models in catalog.items():
        fam: dict[str, list[str]] = {}
        for model in models:
            parts = [p for p in str(model).split() if p]
            core = list(parts)
            while core and core[-1].lower() in variant_tokens:
                core.pop()
            family = " ".join(core).strip() or model
            fam.setdefault(family, [])
            if model not in fam[family]:
                fam[family].append(model)
        out[brand] = fam
    return out


def _signal_display(kind: str) -> str:
    return {
        "buy-strong": "🔥 Prime Opportunity",
        "buy": "🟢 Opportunity",
        "hold": "⚖️ Stable Value",
        "wait": "⚠️ Observation",
        "sell": "❌ Exit Risk",
    }.get(kind, "⚠️ Observation")


def _fmt_arbitrage(item: dict) -> str:
    arb = item.get("arbitrage") or {}
    signal = str(arb.get("signal") or "⚖️ NO ARBITRAGE")
    message = str(arb.get("message") or "")
    diff = float(arb.get("difference") or 0.0)
    profit = float(arb.get("profit_estimate") or 0.0)
    score = int(arb.get("opportunity_score") or 0)
    return f"{signal} | 🌍 {message} | 🔥 Profit: +{profit:.2f}\u00a0€ | 📊 Score: {score}% | Δ {diff:.2f}\u00a0€"


@app.get("/ping")
def ping() -> JSONResponse:
    """Test connectivité sans auth (mobile / opérateur)."""
    return JSONResponse({"ok": True})


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "service": "sneaker_bot",
            "entrypoint": PROD_ENTRYPOINT,
            "data_freshness": _data_freshness_snapshot(),
        }
    )


@app.get("/robots.txt")
def robots() -> PlainTextResponse:
    return PlainTextResponse(
        "User-agent: *\nDisallow: /admin/\nDisallow: /api/\n",
        media_type="text/plain; charset=utf-8",
    )


@app.get("/subscribe", response_class=HTMLResponse)
def subscribe_page() -> HTMLResponse:
    """Parcours abonnement (offre → paiement / essai → confirmation)."""
    path = TEMPLATES_DIR / "subscribe.html"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Page subscribe introuvable")
    html = path.read_text(encoding="utf-8")
    admin_digits = get_admin_whatsapp_digits()
    html = html.replace("https://wa.me/213540388413", f"https://wa.me/{admin_digits}")
    return HTMLResponse(content=html)


@app.post("/subscribe")
async def subscribe_submit(request: Request) -> JSONResponse:
    """Enregistre une demande d'abonnement ; réponse JSON pour le flux sans rechargement."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Corps JSON attendu")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON objet attendu")

    name = str(body.get("name") or "").strip()
    email = str(body.get("email") or "").strip()
    plan = str(body.get("plan") or "").strip().lower()
    reference = str(body.get("reference") or "").strip()
    whatsapp = str(body.get("whatsapp") or "").strip()
    canal = str(body.get("canal") or "email").strip().lower()
    if canal not in ("email", "whatsapp", "both"):
        canal = "email"

    if not name:
        raise HTTPException(status_code=400, detail="Prénom / nom requis")
    if plan not in ("essai", "mensuel", "annuel"):
        raise HTTPException(status_code=400, detail="Plan invalide")

    try:
        sub = submit_subscription(
            name,
            email,
            plan,
            reference,
            whatsapp=whatsapp,
            canal=canal,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        logging.getLogger(__name__).exception("subscribe_submit: %s", e)
        raise HTTPException(status_code=500, detail="Erreur serveur") from e

    sid = str(sub.get("id") or "")
    return JSONResponse(
        {
            "success": True,
            "id": sid,
            "message": "Demande enregistrée. Vous recevez une confirmation sous 24h après vérification.",
        }
    )


@app.get("/admin/subscriptions", response_class=HTMLResponse)
def admin_subscriptions(request: Request):
    role = request.cookies.get("sb_role", "")
    logger.info("ADMIN SUBS: role=[%s]", role)

    if role != "admin":
        return RedirectResponse("/login", status_code=303)

    data = get_all_subscriptions()
    pending_raw = get_pending_subscriptions()
    pending = []
    for row in pending_raw:
        d = dict(row)
        d["wa_me_url"] = build_whatsapp_admin_wa_me_url(d)
        pending.append(d)

    validated: list[dict[str, object]] = []
    for s in data.get("validated") or []:
        if not isinstance(s, dict):
            continue
        sub = dict(s)
        canal_v = str(sub.get("canal") or "email").strip().lower()
        if canal_v not in ("email", "whatsapp", "both"):
            canal_v = "email"
        sub["canal"] = canal_v
        sub["show_email_sent"] = canal_v in ("email", "both") and bool(sub.get("email"))
        wa = str(sub.get("whatsapp") or "").strip()
        if canal_v in ("whatsapp", "both") and wa and sub.get("username"):
            sub["wa_client_url"] = build_whatsapp_client_login_wa_me_url(
                raw_whatsapp=wa,
                username=str(sub.get("username") or ""),
                password=str(sub.get("password") or ""),
            )
        else:
            sub["wa_client_url"] = None
        validated.append(sub)

    rejected = [s for s in (data.get("rejected") or []) if isinstance(s, dict)]

    return templates.TemplateResponse(
        "admin_subscriptions.html",
        {
            "request": request,
            "pending": pending,
            "validated": validated,
            "rejected": rejected,
        },
    )


@app.get("/admin/validate/{sub_id}")
def admin_validate(sub_id: str, request: Request):
    role = request.cookies.get("sb_role", "")
    auth = request.cookies.get("sb_auth", "")

    logger.info(
        "ADMIN VALIDATE: role=[%s] auth=[%s]",
        role,
        (auth[:10] if auth else "VIDE"),
    )

    if role != "admin":
        logger.warning("ADMIN VALIDATE: role manquant → login")
        return RedirectResponse("/login", status_code=303)

    try:
        validate_subscription(sub_id)
        logger.info("✅ Abonnement %s validé", sub_id)
    except Exception as e:
        logger.error("❌ Erreur validation %s: %s", sub_id, e)

    return RedirectResponse("/admin/subscriptions", status_code=303)


@app.get("/admin/reject/{sub_id}")
def admin_reject(sub_id: str, request: Request):
    role = request.cookies.get("sb_role", "")
    if role != "admin":
        return RedirectResponse("/login")
    try:
        reject_subscription(sub_id)
    except Exception:
        pass
    return RedirectResponse("/admin/subscriptions")


def _send_client_password_reset_email(to_addr: str, login: str, new_password: str) -> None:
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_pass = os.getenv("SMTP_PASS", "")
    if not smtp_user or not smtp_pass or not (to_addr or "").strip():
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "SneakerBot — Nouveau mot de passe"
    msg["From"] = smtp_user
    msg["To"] = (to_addr or "").strip()
    safe_login = escape(login)
    safe_pw = escape(new_password)
    html = f"""
    <html><body style="font-family:Arial;background:#0d0d0d;color:#fff;padding:20px">
    <h2 style="color:#00ff88">Mot de passe réinitialisé</h2>
    <p>Votre accès SneakerBot a été mis à jour par l’administrateur.</p>
    <div style="background:#111;border:1px solid #00ff88;border-radius:8px;padding:16px;margin:16px 0">
      <p style="margin:6px 0">👤 <b>Identifiant :</b> <code style="color:#00ff88">{safe_login}</code></p>
      <p style="margin:6px 0">🔑 <b>Nouveau mot de passe :</b> <code style="color:#00ff88">{safe_pw}</code></p>
    </div>
    <p style="color:#888;font-size:13px">Connectez-vous sur le comparateur avec ces identifiants.</p>
    </body></html>
    """
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, msg["To"], msg.as_string())


@app.get("/admin/reset/{username}", response_class=HTMLResponse)
def admin_reset_credentials(username: str, request: Request) -> HTMLResponse:
    role = request.cookies.get("sb_role", "")
    if role != "admin":
        return RedirectResponse("/login")

    uname = str(username or "").strip()
    if not uname:
        return HTMLResponse(
            content="<html><body style='background:#0d0d0d;color:#fff;padding:40px'>"
            "<p>Identifiant manquant.</p><a href='/admin/subscriptions' style='color:#00ff88'>Retour</a></body></html>",
            status_code=400,
        )

    try:
        raw_text = ACCESS_CONTROL_PATH.read_text(encoding="utf-8")
        data = json.loads(raw_text)
    except Exception:
        logging.getLogger(__name__).exception("Lecture access_control pour reset")
        return HTMLResponse(
            content="<html><body style='background:#0d0d0d;color:#fff;padding:40px'>"
            "<p>Fichier d’accès illisible.</p><a href='/admin/subscriptions' style='color:#00ff88'>Retour</a></body></html>",
            status_code=500,
        )

    if not isinstance(data, dict):
        return HTMLResponse(content="Structure invalide", status_code=500)

    users = data.get("users")
    if not isinstance(users, list):
        users = []

    alphabet = string.ascii_letters + string.digits
    new_password = "".join(secrets.choice(alphabet) for _ in range(12))

    target_email = ""
    found = False
    for user in users:
        if not isinstance(user, dict):
            continue
        if str(user.get("username") or "").strip() == uname:
            if str(user.get("role") or "").strip().lower() == "admin":
                return HTMLResponse(
                    content=(
                        "<html><body style='background:#0d0d0d;color:#fff;padding:40px;text-align:center'>"
                        "<p>Impossible de réinitialiser un compte administrateur.</p>"
                        "<a href='/admin/subscriptions' style='color:#00ff88'>← Retour admin</a></body></html>"
                    ),
                    status_code=403,
                )
            user["password"] = _hash_password(new_password)
            target_email = str(user.get("email") or "").strip()
            found = True
            break

    if not found:
        return HTMLResponse(
            content=(
                "<html><body style='background:#0d0d0d;color:#fff;padding:40px;text-align:center'>"
                "<p>Utilisateur introuvable.</p>"
                "<a href='/admin/subscriptions' style='color:#00ff88'>← Retour admin</a></body></html>"
            ),
            status_code=404,
        )

    data["users"] = users
    try:
        ACCESS_CONTROL_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        logging.getLogger(__name__).exception("Écriture access_control après reset")
        return HTMLResponse(
            content="<html><body style='background:#0d0d0d;color:#fff;padding:40px'>"
            "<p>Échec enregistrement.</p><a href='/admin/subscriptions' style='color:#00ff88'>Retour</a></body></html>",
            status_code=500,
        )

    if target_email:
        try:
            _send_client_password_reset_email(target_email, uname, new_password)
        except Exception:
            logging.getLogger(__name__).exception("Email reset mot de passe client")

    u_esc = escape(uname)
    pw_esc = escape(new_password)
    email_note = (
        f"<p style='color:#9ca3af'>Un email a été envoyé à <strong>{escape(target_email)}</strong>.</p>"
        if target_email
        else "<p style='color:#fbbf15'>Aucun email enregistré — communiquez le mot de passe au client.</p>"
    )
    return HTMLResponse(
        content=f"""<!doctype html>
<html lang="fr">
<head><meta charset="utf-8"><title>Reset credentials</title></head>
<body style="background:#0d0d0d;color:#fff;font-family:Arial,sans-serif;padding:40px;text-align:center">
  <h2 style="color:#00ff88">✅ Credentials réinitialisés !</h2>
  <div style="background:#111;border:1px solid #00ff88;border-radius:8px;padding:20px;
              display:inline-block;margin:20px;text-align:left">
    <p>👤 Login : <strong style="color:#00ff88">{u_esc}</strong></p>
    <p>🔑 Nouveau mot de passe : <strong style="color:#00ff88">{pw_esc}</strong></p>
  </div>
  {email_note}
  <p><a href="/admin/subscriptions" style="color:#00ff88">← Retour admin</a></p>
</body>
</html>"""
    )


@app.get("/api/admin/notifications")
def api_admin_notifications(request: Request) -> JSONResponse:
    role = request.cookies.get("sb_role", "")
    if role != "admin":
        return JSONResponse({"detail": "Forbidden"}, status_code=403)
    return JSONResponse({"notifications": get_unsent_admin_notifications()})


def _simple_login_html(
    next_path: str,
    error: str = "",
    *,
    sales_closed: bool = False,
) -> str:
    safe_next = escape(next_path if str(next_path).startswith("/") else "/comparison")
    err_block = f"<div class='error'>❌ {escape(error)}</div>" if error else ""
    closed_block = (
        "<p style='text-align:center;font-size:12px;color:#facc15;margin-bottom:16px;"
        "padding:8px;border:1px solid #854d0e;border-radius:8px;background:#1c1917'>"
        "Accès public fermé : connectez-vous avec un compte client ou admin.</p>"
        if sales_closed
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>SneakerBot — Connexion</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            background: #0d0d0d;
            color: #fff;
            font-family: system-ui, sans-serif;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }}
        .card {{
            background: #111;
            border: 1px solid #222;
            border-radius: 12px;
            padding: 32px;
            width: 100%;
            max-width: 380px;
        }}
        h1 {{
            color: #00ff88;
            text-align: center;
            margin-bottom: 8px;
            font-size: 24px;
        }}
        p.sub {{
            text-align: center;
            color: #666;
            font-size: 13px;
            margin-bottom: 24px;
        }}
        label {{
            display: block;
            color: #aaa;
            font-size: 13px;
            margin-bottom: 4px;
        }}
        input {{
            width: 100%;
            padding: 12px;
            background: #1a1a1a;
            border: 1px solid #333;
            border-radius: 8px;
            color: #fff;
            font-size: 15px;
            margin-bottom: 16px;
        }}
        input:focus {{
            outline: none;
            border-color: #00ff88;
        }}
        button {{
            width: 100%;
            padding: 14px;
            background: #00ff88;
            color: #000;
            border: none;
            border-radius: 8px;
            font-size: 16px;
            font-weight: bold;
            cursor: pointer;
        }}
        .error {{
            background: #ff444422;
            border: 1px solid #ff4444;
            color: #ff6666;
            padding: 10px;
            border-radius: 8px;
            margin-bottom: 16px;
            font-size: 13px;
            text-align: center;
        }}
        .logo {{
            text-align: center;
            font-size: 40px;
            margin-bottom: 8px;
        }}
    </style>
</head>
<body>
    <div class="card">
        <div class="logo">👟</div>
        <h1>SneakerBot</h1>
        <p class="sub">Connectez-vous à votre espace</p>
        {closed_block}
        {err_block}
        <form method="POST" action="/login">
            <input type="hidden" name="next" value="{safe_next}">
            <label>Identifiant</label>
            <input type="text"
                   name="username"
                   placeholder="votre identifiant"
                   autocomplete="username"
                   autocorrect="off"
                   autocapitalize="none"
                   spellcheck="false"
                   required>
            <label>Mot de passe</label>
            <input type="password"
                   name="password"
                   placeholder="votre mot de passe"
                   autocomplete="current-password"
                   required>
            <button type="submit">Se connecter →</button>
        </form>
        <p style="text-align:center;margin-top:16px;font-size:12px;color:#444">
            Pas encore abonné ?
            <a href="/subscribe" style="color:#00ff88">S'abonner</a>
        </p>
    </div>
</body>
</html>"""


def _simple_register_html(error: str = "") -> str:
    err_block = f"<div class='error'>❌ {escape(error)}</div>" if error else ""
    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>SneakerBot — Inscription</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            background: #0d0d0d;
            color: #fff;
            font-family: system-ui, sans-serif;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }}
        .card {{
            background: #111;
            border: 1px solid #222;
            border-radius: 12px;
            padding: 32px;
            width: 100%;
            max-width: 420px;
        }}
        h1 {{ color: #00ff88; text-align: center; margin-bottom: 8px; font-size: 24px; }}
        p.sub {{ text-align: center; color: #666; font-size: 13px; margin-bottom: 24px; }}
        label {{ display: block; color: #aaa; font-size: 13px; margin-bottom: 4px; }}
        input {{
            width: 100%;
            padding: 12px;
            background: #1a1a1a;
            border: 1px solid #333;
            border-radius: 8px;
            color: #fff;
            font-size: 15px;
            margin-bottom: 16px;
        }}
        button {{
            width: 100%;
            padding: 14px;
            background: #00ff88;
            color: #000;
            border: none;
            border-radius: 8px;
            font-size: 16px;
            font-weight: bold;
            cursor: pointer;
        }}
        .error {{
            background: #ff444422;
            border: 1px solid #ff4444;
            color: #ff6666;
            padding: 10px;
            border-radius: 8px;
            margin-bottom: 16px;
            font-size: 13px;
            text-align: center;
        }}
    </style>
</head>
<body>
    <div class="card">
        <h1>Créer un compte</h1>
        <p class="sub">Inscription client SneakerBot</p>
        {err_block}
        <form method="POST" action="/register">
            <label>Identifiant</label>
            <input type="text" name="username" minlength="4" required>
            <label>Mot de passe</label>
            <input type="password" name="password" minlength="6" required>
            <button type="submit">S'inscrire</button>
        </form>
        <p style="text-align:center;margin-top:12px;font-size:12px;color:#444">
            Déjà un compte ? <a href="/login" style="color:#00ff88">Se connecter</a>
        </p>
    </div>
</body>
</html>"""


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request, error: str = Query("")):
    if _is_authenticated(request):
        return RedirectResponse(url="/comparison", status_code=303)
    return HTMLResponse(content=_simple_register_html(error))


@app.post("/register")
async def register_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    ok, err = _register_user(username, password, role="client")
    if not ok:
        return HTMLResponse(content=_simple_register_html(err), status_code=200)
    return RedirectResponse(url="/login?error=Compte%20cre%C3%A9.%20Connectez-vous.", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    next: str = "/comparison",
    error: str = Query(""),
):
    if _is_authenticated(request):
        return RedirectResponse(url="/comparison", status_code=303)
    safe_next = next if str(next).startswith("/") else "/comparison"
    access = _load_access_control()
    sales_closed = str(access.get("sales_mode") or "open").lower() == "closed"
    return HTMLResponse(content=_simple_login_html(safe_next, error, sales_closed=sales_closed))


@app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/comparison"),
):
    log = logging.getLogger(__name__)
    username = str(username or "").strip()
    password = str(password or "").strip()
    # Mobile / clavier : espaces parasites ("khadidja 7854" → khadidja7854)
    username_lookup = "".join(username.split())
    if username_lookup != username:
        log.info("LOGIN username normalisé: [%s] → [%s]", username, username_lookup)
    safe_next = next if str(next).startswith("/") else "/comparison"

    log.info("LOGIN: [%s] essai", username_lookup)

    user = _find_user(username_lookup, password)

    if not user:
        log.warning("LOGIN FAILED: [%s]", username_lookup)
        access = _load_access_control()
        sales_closed = str(access.get("sales_mode") or "open").lower() == "closed"
        return HTMLResponse(
            content=_simple_login_html(
                safe_next, "Identifiants invalides.", sales_closed=sales_closed
            ),
            status_code=200,
        )

    role = str(user.get("role") or "client").strip().lower()
    if role not in ("admin", "client"):
        role = "client"
    log.info("LOGIN OK: [%s] role=%s", username_lookup, role)

    response = RedirectResponse("/comparison", status_code=303)

    cookie_opts: dict[str, object] = {
        "httponly": True,
        "max_age": 43200,
        "path": "/",
        "samesite": "lax",
        "secure": False,  # Important : False pour compatibilité Nginx / proxy
    }

    response.set_cookie("sb_auth", AUTH_TOKEN, **cookie_opts)
    response.set_cookie("sb_role", role, **cookie_opts)
    response.set_cookie("sb_user", username_lookup, **cookie_opts)

    _tok = AUTH_TOKEN or ""
    log.info(
        "LOGIN COOKIES: auth=%s... role=%s user=%s secure=False (proxy / mobile HTTPS)",
        _tok[:8] if len(_tok) >= 8 else _tok,
        role,
        username_lookup,
    )

    return response


@app.get("/logout")
def logout(request: Request):
    resp = RedirectResponse(url="/login", status_code=303)
    # Même profil que set_cookie login (sans Secure) pour que le navigateur supprime bien les cookies.
    resp.delete_cookie("sb_auth", path="/", secure=False, httponly=True, samesite="lax")
    resp.delete_cookie("sb_role", path="/", secure=False, httponly=True, samesite="lax")
    resp.delete_cookie("sb_user", path="/", secure=False, httponly=True, samesite="lax")
    return resp


@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> RedirectResponse:
    return RedirectResponse(url="/comparison", status_code=307)
    snapshot = get_market_snapshot()
    products = snapshot["products"]
    best = max(
        products,
        key=lambda x: float(((x.get("arbitrage") or {}).get("profit_estimate") or 0.0)),
        default=None,
    )
    best_block = ""
    if best is not None:
        best_arb = best.get("arbitrage") or {}
        best_name = escape(str(best.get("product") or "N/A"))
        best_profit = float(best_arb.get("profit_estimate") or 0.0)
        best_buy = escape(str(best_arb.get("buy_country") or "FR"))
        best_sell = escape(str(best_arb.get("sell_country") or "LU"))
        best_block = f"""
        <section class="card" style="border:1px solid #1f9f53; margin-bottom:1rem;">
          <h2 style="margin:0 0 .45rem; font-size:1.2rem;">🔥 BEST OPPORTUNITY TODAY</h2>
          <p style="margin:.2rem 0; font-size:1.05rem;"><strong>{best_name}</strong></p>
          <p style="margin:.2rem 0;">💰 Profit: +{best_profit:.2f}\u00a0€</p>
          <p style="margin:.2rem 0;">🌍 Buy {best_buy} → Sell {best_sell}</p>
        </section>
        """
    cards = []
    for p in products:
        product = escape(str(p.get("product", "")))
        signal_kind = escape(str(p.get("signal_kind", "wait")))
        signal_text = escape(_signal_display(str(p.get("signal_kind", "wait"))))
        avg = float(p.get("avg") or 0.0)
        variation = float(p.get("variation") or 0.0)
        trend = escape(str(p.get("trend", "STABLE")))
        score = int(p.get("score") or 0)
        brand = escape(str(p.get("brand", "UNKNOWN")))
        model = escape(str(p.get("model", "UNKNOWN")))
        country = escape(str(p.get("country", "FR")))
        price_avg = p.get("price_avg") or {}
        fr_avg = float(price_avg.get("FR") or 0.0)
        be_avg = float(price_avg.get("BE") or 0.0)
        lu_avg = float(price_avg.get("LU") or 0.0)
        arb = p.get("arbitrage") or {}
        has_arb = bool(arb.get("opportunity"))
        buy_country = escape(str(arb.get("buy_country") or "FR"))
        sell_country = escape(str(arb.get("sell_country") or "LU"))
        profit_estimate = float(arb.get("profit_estimate") or 0.0)
        opportunity_score = int(arb.get("opportunity_score") or 0)
        if opportunity_score >= 70:
            badge = "🔥 HIGH PROFIT"
        elif opportunity_score >= 40:
            badge = "⚡ MEDIUM"
        else:
            badge = "💤 LOW"
        if has_arb:
            market_message = (
                f"<p class=\"ux-msg\" style=\"margin:.2rem 0;\">🔥 ARBITRAGE OPPORTUNITY</p>"
                f"<p class=\"ux-msg\" style=\"margin:.2rem 0;\">🌍 Buy in {buy_country} → Sell in {sell_country}</p>"
                f"<p class=\"ux-msg\" style=\"margin:.2rem 0;\">💰 Profit: +{profit_estimate:.2f}\u00a0€</p>"
                f"<p class=\"ux-msg\" style=\"margin:.2rem 0 .75rem;\">📊 Score: {opportunity_score}% | {escape(badge)}</p>"
            )
        else:
            market_message = f"<p class=\"ux-msg\" style=\"margin:.25rem 0 .75rem;\">{escape(_fmt_arbitrage(p))}</p>"
        href = f"/product/{quote(str(p.get('product', '')))}"
        cards.append(
            f"""
            <article class="card">
              <div class="row">
                <div class="name">{product}</div>
                <div class="count">Score {score}/5</div>
              </div>
              <div class="meta">
                <div><span class="k">Brand</span><span class="v">{brand}</span></div>
                <div><span class="k">Model</span><span class="v">{model}</span></div>
                <div><span class="k">Country focus</span><span class="v">{country}</span></div>
              </div>
              <p class="signal signal-{signal_kind}">{signal_text}</p>
              <div class="meta">
                <div><span class="k">Prix moyen</span><span class="v">{avg:.2f}\u00a0€</span></div>
                <div><span class="k">Variation</span><span class="v">{variation:.2f}\u00a0€</span></div>
                <div><span class="k">Tendance</span><span class="v">{trend}</span></div>
              </div>
              <table style="width:100%;border-collapse:collapse;margin:.45rem 0 .75rem;">
                <thead><tr><th style="text-align:left;">FR</th><th style="text-align:left;">BE</th><th style="text-align:left;">LU</th></tr></thead>
                <tbody><tr><td>{fr_avg:.2f}\u00a0€</td><td>{be_avg:.2f}\u00a0€</td><td>{lu_avg:.2f}\u00a0€</td></tr></tbody>
              </table>
              {market_message}
              <a class="btn" href="{href}">Get Access</a>
            </article>
            """
        )
    cards_html = "\n".join(cards) if cards else '<article class="card"><p>Aucune donnée disponible.</p></article>'
    models_catalog = _load_models_catalog_from_json()
    brand_keys_home = list(models_catalog.keys())
    first_brand_home = brand_keys_home[0] if brand_keys_home else ""
    first_models_home = models_catalog.get(first_brand_home, [])
    brand_opts_home = "".join(f"<option>{escape(b)}</option>" for b in brand_keys_home)
    model_opts_home = (
        "".join(f'<option value="{escape(m)}">{escape(m)}</option>' for m in first_models_home)
        or '<option value="">—</option>'
    )
    js_home_catalog = json.dumps(models_catalog, ensure_ascii=False)
    return f"""<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Luxury Sneaker Intelligence</title>
  <style>
    :root{{--bg:#0b0b0b;--panel:#111111;--text:#fff;--muted:#b2b2b2;--line:#232323;--buyStrong:#00c896;--buy:#38d39f;--hold:#f2a65a;--sell:#ff5a5f;--wait:#f6d65f;--gold:#b99a5b;}}
    *{{box-sizing:border-box}} body{{margin:0;background:radial-gradient(circle at 15% -10%, #111f1b 0%, var(--bg) 45%);color:var(--text);font-family:ui-sans-serif,system-ui;padding:1.1rem;line-height:1.35;}}
    .wrap{{max-width:1080px;margin:0 auto}} .top{{display:flex;align-items:flex-end;justify-content:space-between;gap:1.1rem;margin-bottom:1.4rem;flex-wrap:wrap}}
    h1{{margin:0;font-size:1.72rem;letter-spacing:.02em;font-weight:700;text-transform:uppercase}} .sub{{margin:.35rem 0 0;color:var(--muted);font-size:.96rem}}
    .count{{color:var(--gold);font-size:.82rem;letter-spacing:.08em;text-transform:uppercase}} .grid{{display:grid;grid-template-columns:1fr;gap:.9rem}}
    .card{{border:1px solid var(--line);border-radius:16px;background:linear-gradient(165deg,#121212,#0f0f0f);padding:1rem;box-shadow:0 16px 32px rgba(0,0,0,.34)}}
    .row{{display:flex;justify-content:space-between;gap:.9rem;align-items:center}} .name{{font-size:1.07rem;font-weight:650}}
    .meta{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:.65rem;margin:.85rem 0 .95rem}} .k{{display:block;font-size:.72rem;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}} .v{{display:block;font-size:1rem;font-weight:700}}
    .signal{{font-size:1.26rem;font-weight:850;letter-spacing:.02em;margin:.3rem 0 .78rem}} .signal-buy-strong{{color:var(--buyStrong)}} .signal-buy{{color:var(--buy)}} .signal-hold{{color:var(--hold)}} .signal-sell{{color:var(--sell)}} .signal-wait{{color:var(--wait)}}
    .btn,.cta{{display:inline-block;text-decoration:none;border-radius:999px;padding:.58rem .92rem;font-weight:700;font-size:.84rem;letter-spacing:.02em}}
    .btn{{border:1px solid #1f9f53;color:#d8ffe8;background:linear-gradient(180deg,#18b886,#0f8e69)}} .cta{{color:#f7fff9;border:1px solid #1f9f53;background:#0f8e69;font-size:.82rem;text-transform:uppercase}}
    .ux-msg{{margin-top:.55rem;color:#d4d4d4;font-size:.9rem}}
    @media (min-width:760px){{body{{padding:1.5rem}} .grid{{grid-template-columns:repeat(2,minmax(0,1fr));gap:1rem}} h1{{font-size:1.94rem}}}}
  </style>
</head>
<body>
  <main class="wrap">
    <div class="top">
      <div>
        <h1>Luxury Sneaker Intelligence</h1>
        <p class="sub">Real-time market signals for premium sneakers</p>
        <p class="ux-msg">Last update: {escape(str(snapshot['last_update']))}</p>
        <p class="ux-msg">Top opportunity today: {escape(str(snapshot['top_opportunity']))}</p>
      </div>
      <div style="display:flex;align-items:center;gap:.7rem;flex-wrap:wrap">
        <div class="count">{len(products)} produits</div>
        <a class="cta" href="/api/products">Get Access</a>
      </div>
    </div>
    <section class="card" style="margin-bottom:1rem;">
      <form method="get" action="/search" style="display:grid;grid-template-columns:1fr 1fr auto;gap:.6rem;align-items:center;" id="search-form">
        <select name="brand" id="search-brand" style="padding:.62rem;border-radius:10px;border:1px solid #2d2d2d;background:#0f0f0f;color:#fff;">
          {brand_opts_home}
        </select>
        <select name="model" id="modelSelect" style="padding:.62rem;border-radius:10px;border:1px solid #2d2d2d;background:#0f0f0f;color:#fff;">
          {model_opts_home}
        </select>
        <button type="submit" class="btn" style="cursor:pointer;">Search</button>
      </form>
      <script>
const catalog = {js_home_catalog};

const brandSelect = document.querySelector("select[name='brand']");
const modelSelect = document.getElementById("modelSelect");

if (brandSelect && modelSelect) {{
  brandSelect.addEventListener("change", () => {{
      const brand = brandSelect.value;

      modelSelect.innerHTML = "";

      if (catalog[brand]) {{
          catalog[brand].forEach(model => {{
              const option = document.createElement("option");
              option.value = model;
              option.textContent = model;
              modelSelect.appendChild(option);
          }});
      }}
  }});
}}
      </script>
    </section>
    {best_block}
    <section class="grid">{cards_html}</section>
  </main>
</body>
</html>"""


@app.get("/product/{product_name:path}", response_class=HTMLResponse)
def product_detail(product_name: str) -> str:
    products = get_market_products()
    key = (product_name or "").strip().lower()
    item = next((p for p in products if str(p.get("product", "")).strip().lower() == key), None)
    if item is None:
        raise HTTPException(status_code=404, detail="Produit introuvable")
    kind = str(item.get("signal_kind", "wait"))
    arb = item.get("arbitrage") or {}
    price_min = item.get("price_min") or {}
    price_max = item.get("price_max") or {}
    price_avg = item.get("price_avg") or {}
    insight = {
        "buy-strong": "This model shows strong upside potential with active demand and favorable pricing momentum.",
        "buy": "This model presents a healthy opportunity with positive momentum and attractive market entry conditions.",
        "hold": "This model shows stable pricing with moderate market activity.",
        "wait": "This model is currently in observation mode with limited conviction from recent market movement.",
        "sell": "This model shows downside pressure and elevated exit risk in current market conditions.",
    }.get(kind, "This model is currently in observation mode with limited conviction from recent market movement.")
    return f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(str(item.get('product', '')))} - Luxury Detail</title></head>
<body style="margin:0;min-height:100vh;background:#0b0b0b;color:#fff;font-family:ui-sans-serif;display:flex;align-items:center;justify-content:center;padding:1rem;">
<article style="width:100%;max-width:720px;border:1px solid #232323;border-radius:18px;background:#111;padding:1.2rem;">
<h1 style="margin:0 0 .5rem;">{escape(str(item.get('product', '')))}</h1>
<p>{escape(_signal_display(kind))}</p>
<p>Prix moyen: {float(item.get('avg') or 0.0):.2f}\u00a0€</p>
<p>Variation: {float(item.get('variation') or 0.0):.2f}\u00a0€</p>
<p>Tendance: {escape(str(item.get('trend') or 'STABLE'))}</p>
<p>Score: {int(item.get('score') or 0)}/5</p>
<p>Brand: {escape(str(item.get('brand') or 'UNKNOWN'))} | Model: {escape(str(item.get('model') or 'UNKNOWN'))}</p>
<p>Shops: {escape(', '.join(item.get('shops') or []))}</p>
<table style="width:100%;border-collapse:collapse;margin:.45rem 0 .75rem;">
  <thead><tr><th style="text-align:left;">Pays</th><th style="text-align:left;">Min</th><th style="text-align:left;">Max</th><th style="text-align:left;">Avg</th></tr></thead>
  <tbody>
    <tr><td>FR</td><td>{float(price_min.get('FR') or 0.0):.2f}\u00a0€</td><td>{float(price_max.get('FR') or 0.0):.2f}\u00a0€</td><td>{float(price_avg.get('FR') or 0.0):.2f}\u00a0€</td></tr>
    <tr><td>BE</td><td>{float(price_min.get('BE') or 0.0):.2f}\u00a0€</td><td>{float(price_max.get('BE') or 0.0):.2f}\u00a0€</td><td>{float(price_avg.get('BE') or 0.0):.2f}\u00a0€</td></tr>
    <tr><td>LU</td><td>{float(price_min.get('LU') or 0.0):.2f}\u00a0€</td><td>{float(price_max.get('LU') or 0.0):.2f}\u00a0€</td><td>{float(price_avg.get('LU') or 0.0):.2f}\u00a0€</td></tr>
  </tbody>
</table>
<p><strong>{escape(str(arb.get('signal') or '⚖️ NO ARBITRAGE'))}</strong></p>
<p>{escape(str(arb.get('message') or 'Buy in FR → Sell in LU'))} | Δ {float(arb.get('difference') or 0.0):.2f}\u00a0€</p>
<p><strong>Market Insight:</strong> {escape(insight)}</p>
<p><a href="/" style="color:#9ae6b4;">← Retour à la liste</a></p>
</article>
</body></html>"""


@app.get("/api/products")
def api_products():
    return get_market_products()


@app.get("/api/catalog")
def api_catalog():
    return {"catalog": get_sneaker_catalog()}


@app.get("/api/catalog/types")
def api_catalog_types():
    catalog = _load_models_catalog_from_json()
    return {"brands": _build_catalog_families(catalog)}


@app.get("/api/opportunities")
def api_opportunities():
    """
    Opportunités arbitrage top 10, triées par différence décroissante.
    """
    return {"opportunities": get_arbitrage_opportunities(limit=10)}


@app.get("/api/control/status")
def api_control_status():
    """
    Statut de pilotage anti-dérive (sans jamais exposer de secrets).
    """
    return AISupervisor().control_status()


@app.get("/api/update/fr/status")
def api_update_fr_status():
    default_payload = {
        "running": False,
        "last_start_at": "",
        "last_end_at": "",
        "last_success": None,
        "last_message": "unknown",
    }
    if not FR_UPDATE_STATUS_PATH.is_file():
        return default_payload
    try:
        raw = json.loads(FR_UPDATE_STATUS_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return default_payload
        payload = dict(default_payload)
        payload.update(raw)
        payload["running"] = bool(payload.get("running"))
        if str(payload.get("last_message") or "") == "scheduler_boot":
            if payload.get("last_success") is True:
                payload["last_message"] = "refresh_termine"
            elif payload.get("last_success") is False:
                payload["last_message"] = "refresh_erreur"
        if not payload["running"]:
            msg = str(payload.get("last_message") or "")
            if "en_cours" in msg:
                if payload.get("last_success") is True:
                    payload["last_message"] = "refresh_termine"
                elif payload.get("last_success") is False:
                    payload["last_message"] = "refresh_erreur"
                else:
                    payload["last_message"] = "refresh_inconnu"
        # Garde-fou UI: si running trop long, on évite un état figé côté écran.
        if payload["running"]:
            try:
                start_raw = str(payload.get("last_start_at") or "").strip()
                start_dt = datetime.strptime(start_raw, "%Y-%m-%d %H:%M:%S")
                if (datetime.now() - start_dt) > timedelta(minutes=35):
                    payload["running"] = False
                    payload["last_message"] = "refresh_timeout_guard"
            except Exception:
                pass
        payload["data_freshness"] = _data_freshness_snapshot()
        return payload
    except Exception:
        return default_payload


@app.get("/api/sources/fr")
def api_sources_fr():
    if not FR_SOURCES_CATALOG_PATH.is_file():
        return {"count": 0, "active_count": 0, "monitoring_count": 0, "disabled_count": 0}
    try:
        raw = json.loads(FR_SOURCES_CATALOG_PATH.read_text(encoding="utf-8"))
        sources = raw if isinstance(raw, list) else []
    except Exception:
        sources = []
    active_count = sum(1 for s in sources if str((s or {}).get("status") or "").strip().lower() == "active")
    monitoring_count = sum(1 for s in sources if str((s or {}).get("status") or "").strip().lower() == "monitoring")
    disabled_count = sum(1 for s in sources if str((s or {}).get("status") or "").strip().lower() == "disabled")
    # Ne pas exposer les noms/domaines des sources en public.
    return {
        "count": len(sources),
        "active_count": active_count,
        "monitoring_count": monitoring_count,
        "disabled_count": disabled_count,
    }


@app.get("/api/comparison/fr")
def api_comparison_fr(
    brand: str = "",
    model: str = "",
    include_excluded: bool = False,
    validated_only: bool = True,
    mask_shops: bool = True,
):
    bq = _normalize_key(brand)
    mq = _normalize_key(model)
    sources_map: dict[str, dict[str, object]] = {}
    if FR_SOURCES_CATALOG_PATH.is_file():
        try:
            raw_sources = json.loads(FR_SOURCES_CATALOG_PATH.read_text(encoding="utf-8"))
            if isinstance(raw_sources, list):
                for s in raw_sources:
                    if not isinstance(s, dict):
                        continue
                    name = str(s.get("name") or "").strip()
                    if name:
                        sources_map[_normalize_key(name)] = s
        except Exception:
            sources_map = {}
    items: list[dict[str, object]] = []
    rows: list[dict[str, str]] = []
    message = ""
    if MARKET_FR_SOURCES_CSV.is_file():
        with MARKET_FR_SOURCES_CSV.open(encoding="utf-8", newline="") as f:
            rows = [dict(r) for r in csv.DictReader(f)]
    elif MARKET_FR_CSV.is_file():
        # Fallback opérationnel: on expose les lignes agrégées FR comme source unique.
        with MARKET_FR_CSV.open(encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                rows.append(
                    {
                        "brand": str(r.get("brand") or "").strip(),
                        "model": str(r.get("model") or "").strip(),
                        "shop": "Agrégé FR",
                        "price_min": str(r.get("price_min") or ""),
                        "price_max": str(r.get("price_max") or ""),
                        "price_avg": str(r.get("price_avg") or ""),
                        "price_count": "1",
                        "updated_at": str(r.get("updated_at") or "").strip(),
                    }
                )
        message = "fallback_market_fr_csv"
    else:
        return {"items": [], "count": 0, "message": "Aucune donnée FR disponible"}

    # Index disponibilité boutiques par modèle (avant filtres d'exposition)
    available_shops_by_model: dict[tuple[str, str], set[str]] = {}
    reference_shops_by_model: dict[tuple[str, str], set[str]] = {}
    for r in rows:
        rb = str(r.get("brand") or "").strip()
        rm = str(r.get("model") or "").strip()
        if not rb or not rm:
            continue
        model_key = (_normalize_key(rb), _normalize_key(rm))
        shop_name = str(r.get("shop") or "").strip()
        if not shop_name:
            continue
        source_meta = sources_map.get(_normalize_key(shop_name), {})
        source_status = str(source_meta.get("status") or "active").strip().lower()
        if source_status == "active":
            reference_shops_by_model.setdefault(model_key, set()).add(_public_shop_alias(shop_name))
            try:
                pcount = int(float(r.get("price_count") or 0))
            except (TypeError, ValueError):
                pcount = 0
            if pcount > 0:
                available_shops_by_model.setdefault(model_key, set()).add(_public_shop_alias(shop_name))

    for r in rows:
            rb = str(r.get("brand") or "").strip()
            rm = str(r.get("model") or "").strip()
            if bq and _normalize_key(rb) != bq:
                continue
            # Matching strict des types: pas de mélange entre variantes.
            if mq and _normalize_key(rm) != mq:
                continue
            try:
                pmin = float(r.get("price_min") or 0.0)
                pmax = float(r.get("price_max") or 0.0)
                pavg = float(r.get("price_avg") or 0.0)
                pcount = int(float(r.get("price_count") or 0))
            except (TypeError, ValueError):
                continue
            shop_name = str(r.get("shop") or "").strip()
            source_meta = sources_map.get(_normalize_key(shop_name), {})
            source_status = str(source_meta.get("status") or "active").strip().lower()
            spread_ratio = (pmax / pmin) if pmin > 0 else 999.0
            has_physical_presence = bool(source_meta.get("physical_presence"))
            try:
                store_count_estimate = int(source_meta.get("store_count_estimate") or 0)
            except (TypeError, ValueError):
                store_count_estimate = 0
            brand_rules = BRAND_VALIDATION_RULES.get(rb, {})
            max_spread_ratio = float(brand_rules.get("max_spread_ratio", STRICT_MAX_SPREAD_RATIO))
            max_midpoint_dev = float(brand_rules.get("max_midpoint_dev", STRICT_MAX_MIDPOINT_DEVIATION_RATIO))

            # Score crédibilité boutique (0-100).
            score = 100
            if pcount <= 0:
                score -= 80
            elif pcount == 1:
                score -= 36
            elif pcount == 2:
                score -= 18
            if spread_ratio > max_spread_ratio:
                score -= 40
            elif spread_ratio > (max_spread_ratio - 0.12):
                score -= 24
            if source_status == "monitoring":
                score -= 10
            elif source_status == "disabled":
                score -= 40
            # Bonus crédibilité pour réseau de magasins réels en France.
            if has_physical_presence:
                score += 6
            if store_count_estimate >= 100:
                score += 5
            elif store_count_estimate >= 20:
                score += 2
            score = max(0, min(100, score))

            credibility = "high" if score >= 80 else "medium" if score >= 55 else "low"
            excluded = bool(source_status == "disabled" or score < 55)
            exclusion_reason = ""
            if source_status == "disabled":
                exclusion_reason = "source_desactivee"
            elif score < 55:
                exclusion_reason = "score_credibilite_faible"

            validation_reasons: list[str] = []
            if pmin <= 0 or pmax <= 0 or pavg <= 0:
                validation_reasons.append("prix_non_positifs")
            if not (pmin <= pavg <= pmax):
                validation_reasons.append("triplet_incoherent")
            trusted_single_sample = (
                pcount == 1
                and source_status == "active"
                and has_physical_presence
                and score >= 88
                and spread_ratio <= max_spread_ratio
            )
            if pcount < STRICT_MIN_SAMPLE_COUNT and not trusted_single_sample:
                validation_reasons.append("echantillon_insuffisant")
            # Micro-règle premium: avec seulement 2 points, on exige un score très élevé.
            if pcount == 2 and score < 91:
                validation_reasons.append("echantillon_moyen_score_faible")
            if spread_ratio > max_spread_ratio:
                validation_reasons.append("ecart_trop_large")
            if source_status != "active":
                validation_reasons.append("source_non_active")
            if shop_name.lower() in {"agrege fr", "manual fr"}:
                validation_reasons.append("source_fallback")
            if score < STRICT_MIN_SCORE:
                validation_reasons.append("score_sous_seuil")
            # Avg trop décentré => suspicion d'outlier résiduel.
            midpoint = (pmin + pmax) / 2.0
            half_span = max((pmax - pmin) / 2.0, 0.01)
            if abs(pavg - midpoint) / half_span > max_midpoint_dev:
                validation_reasons.append("avg_trop_decentre")
            validated = len(validation_reasons) == 0

            if validated_only and not validated:
                if not include_excluded:
                    continue
            elif excluded and not include_excluded:
                continue

            items.append(
                {
                    "brand": rb,
                    "model": rm,
                    "shop": _public_shop_alias(shop_name) if mask_shops else shop_name,
                    "price_min": pmin,
                    "price_max": pmax,
                    "price_avg": pavg,
                    "price_count": pcount,
                    "spread_ratio": round(spread_ratio, 3),
                    "score": score,
                    "credibility": credibility,
                    "excluded": excluded,
                    "exclusion_reason": exclusion_reason,
                    "validated": validated,
                    "validation_reasons": validation_reasons,
                    "source_status": source_status,
                    "physical_presence": has_physical_presence,
                    "store_count_estimate": store_count_estimate,
                    "nb_reference_shops": len(reference_shops_by_model.get((_normalize_key(rb), _normalize_key(rm)), set())),
                    "nb_available_shops": len(available_shops_by_model.get((_normalize_key(rb), _normalize_key(rm)), set())),
                    "updated_at": str(r.get("updated_at") or "").strip(),
                }
            )
    # Toutes les moyennes par modèle (avant dédup) pour position / min du marché.
    price_avgs_by_model: dict[tuple[str, str], list[float]] = defaultdict(list)
    for _it in items:
        _k = (
            str(_it.get("brand") or "").strip().lower(),
            str(_it.get("model") or "").strip().lower(),
        )
        _pa = float(_it.get("price_avg") or 0.0)
        if _pa > 0:
            price_avgs_by_model[_k].append(_pa)
    # Par défaut: une seule ligne par modèle (brand+model normalisés),
    # en conservant la meilleure crédibilité (score max), puis le prix moyen le plus bas.
    best_by_model: dict[tuple[str, str], dict[str, object]] = {}
    for item in items:
        key = (
            str(item.get("brand") or "").strip().lower(),
            str(item.get("model") or "").strip().lower(),
        )
        current = best_by_model.get(key)
        if current is None:
            best_by_model[key] = item
            continue
        current_score = int(current.get("score") or 0)
        candidate_score = int(item.get("score") or 0)
        if candidate_score > current_score:
            best_by_model[key] = item
            continue
        if candidate_score == current_score:
            current_avg = float(current.get("price_avg") or 0.0)
            candidate_avg = float(item.get("price_avg") or 0.0)
            if candidate_avg < current_avg:
                best_by_model[key] = item

    items = list(best_by_model.values())
    # Enrichissement Google Shopping depuis cache SQLite uniquement (aucun appel SerpAPI ici).
    try:
        from app.google_shopping_verifier import get_cached_google_price
    except Exception:
        get_cached_google_price = None  # type: ignore[assignment]
    if get_cached_google_price is not None:
        for item in items:
            bname = str(item.get("brand") or "").strip()
            mname = str(item.get("model") or "").strip()
            our_avg = float(item.get("price_avg") or 0.0)
            g_cached = get_cached_google_price(bname, mname)
            if not g_cached or g_cached.get("google_price") in (None, 0):
                item["google_price"] = None
                item["google_deviation_pct"] = None
                item["google_deviation_signed_pct"] = None
                item["google_badge"] = "none"
                continue
            g_price = float(g_cached.get("google_price") or 0.0)
            item["google_price"] = round(g_price, 2)
            if g_price <= 0:
                item["google_deviation_pct"] = None
                item["google_deviation_signed_pct"] = None
                item["google_badge"] = "none"
                continue
            deviation = abs(((our_avg - g_price) / g_price) * 100.0)
            item["google_deviation_pct"] = round(deviation, 2)
            item["google_deviation_signed_pct"] = round(
                ((our_avg - g_price) / g_price) * 100.0,
                2,
            )
            if deviation < 15.0:
                item["google_badge"] = "ok"
            elif deviation <= 30.0:
                item["google_badge"] = "warn"
            else:
                item["google_badge"] = "bad"
    else:
        for item in items:
            item.setdefault("google_deviation_signed_pct", None)

    for item in items:
        if "google_deviation_signed_pct" not in item:
            item["google_deviation_signed_pct"] = None

    for item in items:
        _mk = (
            str(item.get("brand") or "").strip().lower(),
            str(item.get("model") or "").strip().lower(),
        )
        _avgs = price_avgs_by_model.get(_mk) or []
        _min_avg = min(_avgs) if _avgs else None
        item["position_client"] = _comparison_position_client(
            _avgs,
            float(item.get("price_avg") or 0.0),
        )
        item["recommandation"] = _comparison_recommendation(item, _min_avg)

    items.sort(key=lambda x: (str(x["brand"]).lower(), str(x["model"]).lower(), -float(x.get("score") or 0), float(x.get("price_avg") or 0.0)))
    payload = {"items": items, "count": len(items)}
    if message:
        payload["message"] = message
    return payload


@app.get("/api/quality/fr")
def api_quality_fr():
    """
    Rapport qualité des sources FR (preuve de crédibilité).
    """
    data = api_comparison_fr(include_excluded=True, validated_only=False)
    items = list(data.get("items") or [])
    total = len(items)
    validated = sum(1 for i in items if bool(i.get("validated")))
    non_validated = total - validated
    by_shop: dict[str, dict[str, int]] = {}
    reason_counts: dict[str, int] = {}
    for i in items:
        shop = str(i.get("shop") or "UNKNOWN")
        s = by_shop.setdefault(shop, {"total": 0, "validated": 0, "non_validated": 0})
        s["total"] += 1
        if i.get("validated"):
            s["validated"] += 1
        else:
            s["non_validated"] += 1
        for r in (i.get("validation_reasons") or []):
            reason_counts[str(r)] = reason_counts.get(str(r), 0) + 1
    return {
        "total_rows": total,
        "validated_rows": validated,
        "non_validated_rows": non_validated,
        "validation_rate": round((validated / total) * 100, 2) if total else 0.0,
        "by_shop": by_shop,
        "top_non_validation_reasons": dict(sorted(reason_counts.items(), key=lambda kv: kv[1], reverse=True)[:8]),
    }


@app.get("/api/google/stats")
def api_google_stats():
    """Statistiques des vérifications prix Google Shopping (cache SQLite)."""
    from app.google_shopping_verifier import get_google_stats, init_google_cache

    init_google_cache()
    return get_google_stats()


@app.get("/api/google/recent")
def api_google_recent(limit: int = Query(20, ge=1, le=200)):
    """Dernières comparaisons prix internes vs Google Shopping."""
    from app.google_shopping_verifier import get_recent_verifications, init_google_cache

    init_google_cache()
    return {"verifications": get_recent_verifications(limit)}


@app.post("/api/google/verify")
async def api_google_verify_now(sample_size: int = Query(5, ge=1, le=5)):
    """Lance un batch Playwright (max 5 modèles) en thread pour ne pas bloquer l’event loop."""
    if not API_LIVE_GOOGLE_VERIFY_ENABLED:
        raise HTTPException(
            status_code=503,
            detail="API live verify désactivée en mode stabilité (scheduler only)",
        )
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    from app.google_shopping_verifier import init_google_cache, run_google_verification_batch

    init_google_cache()
    loop = asyncio.get_event_loop()

    def _run() -> dict:
        return run_google_verification_batch(sample_size)

    with ThreadPoolExecutor(max_workers=1) as pool:
        result = await loop.run_in_executor(pool, _run)
    return result


@app.get("/api/sitemap/test")
def api_sitemap_test(brand: str = Query(...), model: str = Query(...)):
    """Diagnostic prix sitemap (cache 6 h)."""
    from scrapers.sitemap_scraper import get_sitemap_prices, init_sitemap_db

    init_sitemap_db()
    prices = get_sitemap_prices(brand, model)
    return {
        "brand": brand,
        "model": model,
        "sitemap_prices": prices,
        "nb_prices": len(prices),
    }


def _freshness_score_from_updated_at(updated_at: str) -> int:
    try:
        dt = datetime.strptime((updated_at or "").strip(), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return 40
    delta = datetime.now() - dt
    if delta <= timedelta(hours=1):
        return 100
    if delta <= timedelta(hours=3):
        return 80
    if delta <= timedelta(hours=6):
        return 60
    return 40


def _score_grade(score: float) -> str:
    if score >= 85:
        return "Premium"
    if score >= 70:
        return "Fiable"
    if score >= 55:
        return "A surveiller"
    return "Risque eleve"


@app.get("/api/scorecard/fr")
def api_scorecard_fr(
    brand: str = "",
    model: str = "",
    product_quality_score: int = 75,
    performance_score: int = 75,
):
    """
    Scorecard commercial unique (0-100):
    - Qualité produit (manuel): 25%
    - Performance usage (manuel): 20%
    - Crédibilité data prix (automatique): 40%
    - Cohérence marché (spread/disponibilité): 10%
    - Fraîcheur data (automatique): 5%
    """
    product_quality_score = max(0, min(100, int(product_quality_score)))
    performance_score = max(0, min(100, int(performance_score)))

    data = api_comparison_fr(
        brand=brand,
        model=model,
        validated_only=True,
        include_excluded=False,
    )
    items = list(data.get("items") or [])
    if not items:
        logger.warning(
            "Scorecard=0 raison=dataset_empty_or_filtered brand=%s model=%s validated_only=true include_excluded=false",
            brand or "*",
            model or "*",
        )
        return {
            "total_score": 0,
            "grade": "Risque eleve",
            "message": "Aucune ligne valide pour ce filtre",
            "zero_reason": "dataset_empty_or_filtered",
            "weights": {
                "product_quality": 25,
                "performance": 20,
                "data_credibility": 40,
                "market_consistency": 10,
                "freshness": 5,
            },
            "items_count": 0,
        }

    avg_data_cred = round(sum(float(i.get("score") or 0) for i in items) / len(items), 2)
    consistency_points: list[float] = []
    freshness_points: list[float] = []
    for i in items:
        spread = float(i.get("spread_ratio") or 999.0)
        nb_ref = max(1, int(i.get("nb_reference_shops") or 1))
        nb_av = int(i.get("nb_available_shops") or 0)
        coverage = min(1.0, nb_av / nb_ref)
        # 70% spread + 30% couverture.
        spread_component = 100.0 if spread <= 1.35 else 80.0 if spread <= 1.55 else 60.0 if spread <= 1.75 else 40.0
        consistency_points.append(round((spread_component * 0.7) + (coverage * 100.0 * 0.3), 2))
        freshness_points.append(float(_freshness_score_from_updated_at(str(i.get("updated_at") or ""))))

    market_consistency = round(sum(consistency_points) / len(consistency_points), 2)
    freshness_score = round(sum(freshness_points) / len(freshness_points), 2)

    total_score = round(
        (product_quality_score * 0.25)
        + (performance_score * 0.20)
        + (avg_data_cred * 0.40)
        + (market_consistency * 0.10)
        + (freshness_score * 0.05),
        2,
    )
    if total_score <= 0:
        logger.warning(
            "Scorecard=0 raison=extreme_penalties brand=%s model=%s pqs=%d perf=%d cred=%.2f consistency=%.2f freshness=%.2f",
            brand or "*",
            model or "*",
            product_quality_score,
            performance_score,
            avg_data_cred,
            market_consistency,
            freshness_score,
        )

    return {
        "brand": brand,
        "model": model,
        "items_count": len(items),
        "subscores": {
            "product_quality_score": product_quality_score,
            "performance_score": performance_score,
            "data_credibility_score": avg_data_cred,
            "market_consistency_score": market_consistency,
            "freshness_score": freshness_score,
        },
        "weights": {
            "product_quality": 25,
            "performance": 20,
            "data_credibility": 40,
            "market_consistency": 10,
            "freshness": 5,
        },
        "total_score": total_score,
        "zero_reason": "none" if total_score > 0 else "extreme_penalties_or_invalid_data",
        "grade": _score_grade(total_score),
        "legend": {
            "Premium": ">= 85",
            "Fiable": "70-84.99",
            "A surveiller": "55-69.99",
            "Risque eleve": "< 55",
        },
    }


@app.get("/comparison", response_class=HTMLResponse)
def comparison_page(brand: str = "", model: str = "") -> HTMLResponse:
    b = brand.strip()
    m = model.strip()
    # Landing commerciale: préselection d'un modèle vitrine avec données.
    if not b and not m:
        b = "Adidas"
        m = "Samba Classic"
    catalog = _load_models_catalog_from_json()
    brand_keys = list(catalog.keys())
    selected_brand = b if b in catalog else (brand_keys[0] if brand_keys else "")
    selected_models = catalog.get(selected_brand, [])
    selected_model = m if m in selected_models else (selected_models[0] if selected_models else "")
    last_update_label, last_update_state = _fr_last_update_meta()
    clock_state, clock_label = _fr_clock_health()
    state_label = {"fresh": "frais", "aging": "recent", "stale": "ancien"}.get(
        last_update_state, "ancien"
    )
    brand_options = "".join(
        f"<option value='{escape(x)}' {'selected' if x == selected_brand else ''}>{escape(x)}</option>"
        for x in brand_keys
    )
    model_options = "".join(
        f"<option value='{escape(x)}' {'selected' if x == selected_model else ''}>{escape(x)}</option>"
        for x in selected_models
    )
    js_catalog = json.dumps(catalog, ensure_ascii=False)
    html = f"""<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Comparateur FR</title>
  <style>
    body{{margin:0;background:#0b0b0b;color:#fff;font-family:ui-sans-serif,system-ui,Arial,sans-serif;padding:20px}}
    .wrap{{max-width:1140px;margin:0 auto}}
    h1{{margin:0 0 6px;font-size:1.55rem}} .sub{{color:#9ca3af;margin:0 0 14px}}
    .f{{display:flex;gap:10px;flex-wrap:wrap;margin:12px 0 14px}}
    .table-wrap{{width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch;border-radius:12px}}
    input{{background:#121212;border:1px solid #2a2a2a;color:#fff;border-radius:10px;padding:10px 12px;min-width:220px}}
    button{{background:#16a34a;border:0;color:#06210f;border-radius:10px;padding:10px 14px;font-weight:800;cursor:pointer}}
    table#cmp-table{{width:100%;min-width:1100px;border-collapse:collapse;background:#101010;border:1px solid #1f2937;border-radius:12px;overflow:hidden;table-layout:auto}}
    th,td{{padding:10px;border-bottom:1px solid #1f2937;text-align:left}}
    table#cmp-table th.min, table#cmp-table th.moyen, table#cmp-table th.max {{
      white-space: nowrap !important;
    }}
    td.cmp-price {{
      white-space: nowrap !important;
    }}
    .cmp-price-inner {{
      display: inline-block;
      white-space: nowrap !important;
      word-break: keep-all;
    }}
    th{{font-size:.78rem;color:#93c5fd;letter-spacing:.04em;text-transform:uppercase}}
    .pill{{padding:3px 8px;border-radius:999px;font-size:.73rem;font-weight:700}}
    .h{{background:#0f2f1f;color:#4dff9f}} .m{{background:#2e260d;color:#ffd66f}} .l{{background:#3b1111;color:#fda4af}}
    .upd{{display:flex;align-items:center;gap:8px;color:#9ca3af;margin:-8px 0 12px}}
    .upd .spacer{{flex:1}}
    .upd-badge{{padding:2px 8px;border-radius:999px;font-size:.72rem;font-weight:700;letter-spacing:.02em;text-transform:uppercase}}
    .upd-fresh{{background:#0f2f1f;color:#4dff9f}}
    .upd-aging{{background:#2e260d;color:#ffd66f}}
    .upd-stale{{background:#3b1111;color:#fda4af}}
    .job-idle{{background:#1f2937;color:#cbd5e1}}
    .job-run{{background:#0f2f1f;color:#4dff9f}}
    .clock-ok{{background:#0f2f1f;color:#4dff9f}}
    .clock-warning{{background:#2e260d;color:#ffd66f}}
    .clock-critical{{background:#3b1111;color:#fda4af}}
    .logout-link{{color:#e5e7eb;text-decoration:none;border:1px solid #2a2a2a;border-radius:8px;padding:5px 9px;font-size:.78rem;background:#111827}}
    .scorecard{{display:flex;align-items:center;justify-content:space-between;gap:12px;background:#0f1115;border:1px solid #1f2937;border-radius:12px;padding:10px 12px;margin:0 0 12px}}
    .score-main{{display:flex;align-items:center;gap:10px;flex-wrap:wrap}}
    .score-val{{font-size:1.2rem;font-weight:800;color:#e5e7eb}}
    .score-sub{{color:#94a3b8;font-size:.82rem}}
    .score-grade{{padding:3px 9px;border-radius:999px;font-size:.74rem;font-weight:800;text-transform:uppercase;letter-spacing:.02em}}
    .g-premium{{background:#0f2f1f;color:#4dff9f}}
    .g-fiable{{background:#1f3a56;color:#93c5fd}}
    .g-watch{{background:#2e260d;color:#ffd66f}}
    .g-risk{{background:#3b1111;color:#fda4af}}
    .score-ctrl{{display:flex;align-items:center;gap:8px;flex-wrap:wrap}}
    .score-ctrl label{{color:#cbd5e1;font-size:.78rem;display:flex;align-items:center;gap:6px}}
    .score-ctrl input{{width:64px;min-width:64px;background:#121212;border:1px solid #2a2a2a;color:#fff;border-radius:8px;padding:6px 8px}}
    .sync-link{{color:#e5e7eb;text-decoration:none;border:1px solid #2a2a2a;border-radius:8px;padding:5px 9px;font-size:.78rem;background:#111827;cursor:pointer}}
    #sb-mascotte-fixed{{
      position:fixed;top:12px;right:12px;z-index:5000;
      display:flex;flex-direction:column;align-items:flex-end;gap:4px;
      pointer-events:none;
    }}
    #sb-mascotte-fixed > *{{pointer-events:auto}}
    #sb-mascotte-fixed .sb-mascotte-fixed-face{{font-size:2.35rem;line-height:1;filter:drop-shadow(0 2px 8px rgba(0,0,0,.9));}}
    #sb-mascotte-fixed .sb-mascotte-fixed-badge{{
      font-size:.68rem;font-weight:800;color:#052818;background:#4dff9f;border-radius:999px;padding:4px 10px;
      border:1px solid #22c55e;white-space:nowrap;
    }}
    .btn-cmp-hist{{
      background:#0f2f1f;color:#4dff9f;border:1px solid #166534;border-radius:8px;
      padding:6px 12px;font-size:.78rem;font-weight:800;cursor:pointer;white-space:nowrap;
    }}
    .btn-cmp-hist:hover{{filter:brightness(1.12);border-color:#4dff9f}}
    .badge-rec,.badge-pos{{display:inline-block;padding:4px 10px;border-radius:999px;font-size:.72rem;font-weight:800;white-space:nowrap;max-width:100%;overflow:hidden;text-overflow:ellipsis}}
    .rec-gold{{background:#FFD700;color:#1a1200}}
    .rec-gm{{background:#00ff88;color:#06210f}}
    .rec-mkt{{background:#00aaff;color:#fff}}
    .rec-high{{background:#ff8c00;color:#1a0a00}}
    .rec-ok{{background:#888;color:#fff}}
    .pos-t10{{background:#14532d;color:#bbf7d0}}
    .pos-t25{{background:#22c55e;color:#052e16}}
    .pos-mid{{background:#1e40af;color:#93c5fd}}
    .pos-hi{{background:#c2410c;color:#ffedd5}}
    @media (max-width: 768px) {{
      body{{padding:12px}}
      h1{{font-size:1.25rem}}
      .sub{{font-size:.92rem}}
      .upd{{font-size:.9rem}}
      .scorecard{{flex-direction:column;align-items:flex-start}}
      .f{{gap:8px}}
      .f select, .f button{{width:100%;min-width:0}}
      table{{min-width:1100px}}
      th,td{{padding:9px;white-space:nowrap}}
    }}
  </style>
</head>
<body>
  <div id="sb-mascotte-fixed" title="Suivi des prix sneakers — marché FR">
    <span class="sb-mascotte-fixed-face" aria-hidden="true">👟</span>
    <span class="sb-mascotte-fixed-badge">Prix FR</span>
  </div>
  <main class="wrap">
    <h1>🛒 Comparateur FR multi-boutiques</h1>
    <p class="sub">Comparaison crédible des prix sneakers/sport par boutique e-commerce opérant en France.</p>
    <div class="upd">
      <span>Dernière mise à jour : <strong>{last_update_label}</strong></span>
      <span class="upd-badge upd-{last_update_state}">{state_label}</span>
      <span id="clockHealth" class="upd-badge clock-{clock_state}">{clock_label}</span>
      <span id="jobStatus" class="upd-badge job-idle">idle</span>
      <span class="spacer"></span>
      <a class="logout-link" href="/logout">Deconnexion</a>
    </div>
    <div class="scorecard">
      <div class="score-main">
        <span class="score-val" id="scoreTotal">Score global commercial: --/100</span>
        <span class="score-grade g-risk" id="scoreGrade">N/A</span>
        <span class="score-sub" id="scoreMeta">En attente...</span>
      </div>
      <div class="score-ctrl">
        <label>Qualité produit <input type="number" id="pqsInput" min="0" max="100" value="80"></label>
        <label>Performance <input type="number" id="perfInput" min="0" max="100" value="78"></label>
        <button type="button" id="copySyncLink" class="sync-link">Copier lien mobile</button>
        <button type="button" id="shareWhatsApp" class="sync-link">Partager WhatsApp</button>
      </div>
    </div>
    <form class="f" method="get" action="/comparison">
      <select name="brand" id="brandSelect" style="background:#121212;border:1px solid #2a2a2a;color:#fff;border-radius:10px;padding:10px 12px;min-width:220px">{brand_options}</select>
      <select name="model" id="modelSelect" style="background:#121212;border:1px solid #2a2a2a;color:#fff;border-radius:10px;padding:10px 12px;min-width:220px">{model_options}</select>
      <button type="submit">Comparer</button>
    </form>
    <div class="table-wrap">
      <table id="cmp-table">
        <thead><tr><th>Produit</th><th>Boutique</th><th class="min">Min</th><th class="moyen">Moyen</th><th class="max">Max</th><th>Score</th><th>Crédibilité</th><th>Google</th><th>Recommandation</th><th>Position</th><th>Historique</th></tr></thead>
        <tbody id="rows"><tr><td colspan="11" style="color:#9ca3af">Chargement…</td></tr></tbody>
      </table>
    </div>
  </main>
  <div id="modal-history-overlay" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.85);z-index:12000;justify-content:center;align-items:center;padding:16px">
    <div style="background:#1a1a1a;border:1px solid #333;border-radius:12px;padding:24px;width:90%;max-width:600px;max-height:80vh;overflow-y:auto;position:relative">
      <button type="button" onclick="window.__cmpCloseHistory && window.__cmpCloseHistory()" style="position:absolute;top:12px;right:12px;background:#333;color:#fff;border:none;border-radius:50%;width:28px;height:28px;cursor:pointer;font-size:16px">✕</button>
      <h3 style="color:#00ff88;margin:0 0 16px">📊 Historique des prix — <span id="history-modal-title"></span></h3>
      <div id="history-modal-body"><p style="color:#666">Chargement...</p></div>
    </div>
  </div>
  <script>
    const catalog = {js_catalog};
    const brandSelect = document.getElementById('brandSelect');
    const modelSelect = document.getElementById('modelSelect');
    if (brandSelect && modelSelect) {{
      brandSelect.addEventListener('change', () => {{
        const models = catalog[brandSelect.value] || [];
        modelSelect.innerHTML = '';
        models.forEach(m => {{
          const opt = document.createElement('option');
          opt.value = m; opt.textContent = m;
          modelSelect.appendChild(opt);
        }});
      }});
    }}
    const qs = new URLSearchParams({{
      brand:(brandSelect && brandSelect.value) || {selected_brand!r},
      model:(modelSelect && modelSelect.value) || {selected_model!r},
      validated_only:'true'
    }});
    const rows = document.getElementById('rows');
    const jobStatus = document.getElementById('jobStatus');
    const clockHealth = document.getElementById('clockHealth');
    const scoreTotal = document.getElementById('scoreTotal');
    const scoreGrade = document.getElementById('scoreGrade');
    const scoreMeta = document.getElementById('scoreMeta');
    const pqsInput = document.getElementById('pqsInput');
    const perfInput = document.getElementById('perfInput');
    const copySyncLinkBtn = document.getElementById('copySyncLink');
    const shareWhatsAppBtn = document.getElementById('shareWhatsApp');
    function renderJobStatus(s) {{
      if (!jobStatus) return;
      const running = !!(s && s.running);
      jobStatus.className = 'upd-badge ' + (running ? 'job-run' : 'job-idle');
      if (running) {{
        jobStatus.textContent = 'mise à jour en cours...';
        if (clockHealth) {{
          clockHealth.className = 'upd-badge clock-warning';
          clockHealth.textContent = 'horloge active';
        }}
      }} else {{
        const endAt = (s && s.last_end_at) ? String(s.last_end_at).slice(11,16) : '';
        jobStatus.textContent = endAt ? `dernier run ${{endAt}}` : 'idle';
      }}
    }}
    let jobStatusTimer = null;
    function refreshJobStatus() {{
      fetch('/api/update/fr/status')
        .then(r => {{
          if (r.status === 401) {{
            if (jobStatusTimer) {{
              clearInterval(jobStatusTimer);
              jobStatusTimer = null;
            }}
            if (jobStatus) {{
              jobStatus.className = 'upd-badge job-idle';
              jobStatus.textContent = 'session expiree';
            }}
            return null;
          }}
          return r.json();
        }})
        .then(data => {{
          if (data) renderJobStatus(data);
        }})
        .catch(() => {{
          if (jobStatus) {{
            jobStatus.className = 'upd-badge job-idle';
            jobStatus.textContent = 'statut indisponible';
          }}
        }});
    }}
    refreshJobStatus();
    jobStatusTimer = setInterval(refreshJobStatus, 15000);

    function gradeClass(grade) {{
      if (grade === 'Premium') return 'score-grade g-premium';
      if (grade === 'Fiable') return 'score-grade g-fiable';
      if (grade === 'A surveiller') return 'score-grade g-watch';
      return 'score-grade g-risk';
    }}
    // Persistance des champs scorecard (évite impression de cases figées au reload).
    const urlParams = new URLSearchParams(window.location.search);
    const savedPqs = urlParams.get('pqs') || localStorage.getItem('scorecard_pqs') || '80';
    const savedPerf = urlParams.get('perf') || localStorage.getItem('scorecard_perf') || '78';
    if (pqsInput) pqsInput.value = String(savedPqs);
    if (perfInput) perfInput.value = String(savedPerf);
    function currentStateParams() {{
      const p = new URLSearchParams(window.location.search);
      p.set('brand', (brandSelect && brandSelect.value) || {selected_brand!r});
      p.set('model', (modelSelect && modelSelect.value) || {selected_model!r});
      p.delete('include_excluded');
      if (pqsInput) p.set('pqs', String(Math.max(0, Math.min(100, Number(pqsInput.value || '80')))));
      if (perfInput) p.set('perf', String(Math.max(0, Math.min(100, Number(perfInput.value || '78')))));
      return p;
    }}
    function syncUrlState() {{
      const p = currentStateParams();
      window.history.replaceState(null, '', `${{window.location.pathname}}?${{p.toString()}}`);
    }}

    function refreshCommercialScore() {{
      const pqs = Math.max(0, Math.min(100, Number((pqsInput && pqsInput.value) || 80)));
      const perf = Math.max(0, Math.min(100, Number((perfInput && perfInput.value) || 78)));
      localStorage.setItem('scorecard_pqs', String(Math.round(pqs)));
      localStorage.setItem('scorecard_perf', String(Math.round(perf)));
      const brand = (brandSelect && brandSelect.value) || {selected_brand!r};
      const model = (modelSelect && modelSelect.value) || {selected_model!r};
      const s = new URLSearchParams({{
        brand,
        model,
        product_quality_score: String(Math.round(pqs)),
        performance_score: String(Math.round(perf)),
      }});
      fetch('/api/scorecard/fr?' + s.toString())
        .then(r => r.json())
        .then(d => {{
          const total = Number(d.total_score || 0).toFixed(2);
          const grade = String(d.grade || 'Risque eleve');
          const n = Number(d.items_count || 0);
          if (scoreTotal) scoreTotal.textContent = `Score global commercial: ${{total}}/100`;
          if (scoreGrade) {{
            scoreGrade.className = gradeClass(grade);
            scoreGrade.textContent = grade;
          }}
          if (scoreMeta) scoreMeta.textContent = n > 0 ? `${{n}} ligne(s) validée(s) utilisées` : 'Aucune ligne valide';
        }})
        .catch(() => {{
          if (scoreTotal) scoreTotal.textContent = 'Score global commercial: indisponible';
          if (scoreGrade) {{
            scoreGrade.className = 'score-grade g-risk';
            scoreGrade.textContent = 'N/A';
          }}
          if (scoreMeta) scoreMeta.textContent = 'Erreur de calcul scorecard';
        }});
    }}
    refreshCommercialScore();
    if (brandSelect) brandSelect.addEventListener('change', refreshCommercialScore);
    if (modelSelect) modelSelect.addEventListener('change', refreshCommercialScore);
    if (pqsInput) pqsInput.addEventListener('input', refreshCommercialScore);
    if (perfInput) perfInput.addEventListener('input', refreshCommercialScore);
    if (brandSelect) brandSelect.addEventListener('change', syncUrlState);
    if (modelSelect) modelSelect.addEventListener('change', syncUrlState);
    if (pqsInput) pqsInput.addEventListener('input', syncUrlState);
    if (perfInput) perfInput.addEventListener('input', syncUrlState);
    if (copySyncLinkBtn) {{
      copySyncLinkBtn.addEventListener('click', async () => {{
        try {{
          syncUrlState();
          await navigator.clipboard.writeText(window.location.href);
          copySyncLinkBtn.textContent = 'Lien copié';
          setTimeout(() => {{ copySyncLinkBtn.textContent = 'Copier lien mobile'; }}, 1400);
        }} catch {{
          copySyncLinkBtn.textContent = 'Copie impossible';
          setTimeout(() => {{ copySyncLinkBtn.textContent = 'Copier lien mobile'; }}, 1400);
        }}
      }});
    }}
    if (shareWhatsAppBtn) {{
      shareWhatsAppBtn.addEventListener('click', () => {{
        try {{
          syncUrlState();
          const shareUrl = window.location.href;
          // Lien texte uniquement (pas d'image envoyée par le bouton).
          const txt = encodeURIComponent(shareUrl);
          const waUrl = `https://api.whatsapp.com/send?text=${{txt}}`;
          window.open(waUrl, '_blank');
        }} catch {{
          // no-op
        }}
      }});
    }}
    // Propager pqs/perf dans l'URL à chaque soumission du formulaire.
    const form = document.querySelector('form.f');
    if (form) {{
      form.addEventListener('submit', (e) => {{
        e.preventDefault();
        const p = currentStateParams();
        window.location.search = p.toString();
      }});
    }}

    function escAttr(s) {{
      return String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/"/g,'&quot;');
    }}
    function recBadgeClass(r) {{
      const m = {{
        '⭐ MEILLEUR PRIX': 'badge-rec rec-gold',
        '💰 PRIX LE PLUS BAS': 'badge-rec rec-gm',
        '✅ PRIX MARCHÉ': 'badge-rec rec-mkt',
        '⚠️ PRIX ÉLEVÉ': 'badge-rec rec-high',
        '👍 BON PRIX': 'badge-rec rec-ok'
      }};
      return m[r] || 'badge-rec rec-ok';
    }}
    function posBadgeClass(p) {{
      const m = {{
        'Top 10%': 'badge-pos pos-t10',
        'Top 25%': 'badge-pos pos-t25',
        'Milieu de gamme': 'badge-pos pos-mid',
        'Haut de gamme': 'badge-pos pos-hi'
      }};
      return m[p] || 'badge-pos pos-mid';
    }}
    function sneakerModelId(brand, model) {{
      return String(brand || '').trim() + '|' + String(model || '').trim();
    }}
    function cmpCloseHistory() {{
      const ho = document.getElementById('modal-history-overlay');
      if (ho) ho.style.display = 'none';
    }}
    window.__cmpCloseHistory = cmpCloseHistory;
    function cmpRenderHistory(hist, trend) {{
      hist = hist || {{}};
      trend = trend || {{}};
      const points = hist.history || [];
      const trendIcons = {{ hausse: '📈', baisse: '📉', stable: '➡️' }};
      const trendColors = {{ hausse: '#ff6b6b', baisse: '#00ff88', stable: '#ffd700' }};
      const trKey = trend.trend;
      const icon = trendIcons[trKey] || '—';
      const color = trendColors[trKey] || '#aaa';
      const chg = trend.change_pct != null ? Number(trend.change_pct) : 0;
      let svgChart = '<p style="color:#666;font-size:13px">Pas assez de données pour le graphique (min. 2 snapshots)</p>';
      if (points.length >= 2) {{
        const prices = points.map(p => Number(p.price_avg));
        const minP = Math.min(...prices);
        const maxP = Math.max(...prices);
        const range = (maxP - minP) || 1;
        const W = 520, H = 80;
        const pts = prices.map((p, i) => {{
          const x = (i / (prices.length - 1)) * W;
          const y = H - ((p - minP) / range) * (H - 10) - 5;
          return x.toFixed(1) + ',' + y.toFixed(1);
        }}).join(' ');
        const dates = points.map(p => (p.recorded_at && String(p.recorded_at).substring(0, 10)) || '');
        let circles = '';
        for (let i = 0; i < points.length; i++) {{
          const x = (i / (prices.length - 1)) * W;
          const y = H - ((prices[i] - minP) / range) * (H - 10) - 5;
          const dshort = dates[i] ? dates[i].substring(5) : '';
          circles += '<circle cx="' + x.toFixed(1) + '" cy="' + y.toFixed(1) + '" r="3" fill="#00ff88"/>';
          circles += '<text x="' + x.toFixed(1) + '" y="' + (H + 15) + '" font-size="9" fill="#666" text-anchor="middle">' + escAttr(dshort) + '</text>';
        }}
        svgChart = '<svg width="100%" viewBox="0 0 ' + W + ' ' + (H + 20) + '" style="margin:12px 0;overflow:visible">' +
          '<polyline points="' + pts + '" fill="none" stroke="#00ff88" stroke-width="2"/>' + circles + '</svg>';
      }}
      const hb = document.getElementById('history-modal-body');
      if (!hb) return;
      const tlab = trKey != null ? String(trKey) : '—';
      hb.innerHTML = '<div style="display:flex;gap:16px;flex-wrap:wrap;font-size:13px;margin-bottom:16px">' +
        '<span>Tendance : <strong style="color:' + color + '">' + icon + ' ' + escAttr(tlab) + '</strong></span>' +
        '<span>Variation : <strong style="color:' + color + '">' + (chg >= 0 ? '+' : '') + chg + '%</strong></span>' +
        '<span>Min 30j : <strong style="color:#00ff88">' + escAttr(String(trend.min_30d != null ? trend.min_30d : '—')) + '\u00a0€</strong></span>' +
        '<span>Max 30j : <strong style="color:#00ff88">' + escAttr(String(trend.max_30d != null ? trend.max_30d : '—')) + '\u00a0€</strong></span>' +
        '<span>Snapshots : <strong>' + escAttr(String(trend.nb_snapshots != null ? trend.nb_snapshots : 0)) + '</strong></span>' +
        '</div>' + svgChart +
        (points.length === 0 ? '<p style="color:#666;font-size:13px">Aucun historique pour l’instant.</p>' : '');
    }}
    function cmpOpenHistory(modelId) {{
      const ho = document.getElementById('modal-history-overlay');
      if (!ho || !modelId) return;
      ho.style.display = 'flex';
      const ht = document.getElementById('history-modal-title');
      if (ht) ht.textContent = String(modelId).split('|').join(' ');
      const hb = document.getElementById('history-modal-body');
      if (hb) hb.innerHTML = '<p style="color:#666">Chargement...</p>';
      const enc = encodeURIComponent(modelId);
      Promise.all([
        fetch('/api/sneakers/' + enc + '/history?days=30', {{ cache: 'no-store' }}).then(r => {{ if (!r.ok) throw new Error('h'); return r.json(); }}),
        fetch('/api/sneakers/' + enc + '/trend', {{ cache: 'no-store' }}).then(r => {{ if (!r.ok) throw new Error('t'); return r.json(); }})
      ]).then((pair) => cmpRenderHistory(pair[0], pair[1]))
        .catch(() => {{ if (hb) hb.innerHTML = '<p style="color:#ff4444">Historique non disponible</p>'; }});
    }}

    function renderRows(arr, relaxed=false) {{
      if (!arr.length) {{
        rows.innerHTML = '<tr><td colspan="11" style="color:#9ca3af">Aucune donnée pour ce filtre. Lancez d\\'abord run_market(\\'FR\\').</td></tr>';
        return;
      }}
      const note = relaxed
        ? '<tr><td colspan="11" style="color:#facc15;font-size:.82rem;">Mode fallback activé: résultats affichés hors validation stricte.</td></tr>'
        : '';
      rows.innerHTML = note + arr.map(it => {{
        const c = it.credibility === 'high' ? 'h' : (it.credibility === 'medium' ? 'm' : 'l');
        const score = Number(it.score || 0);
        const reason = it.excluded ? ` (exclu: ${{it.exclusion_reason || 'raison_inconnue'}})` : '';
        const gBadge = String(it.google_badge || 'none');
        const gDev = (it.google_deviation_pct == null) ? null : Number(it.google_deviation_pct);
        let gCell = '—';
        if (gBadge === 'ok') {{
          gCell = '🟢 Google ✅';
        }} else if (gBadge === 'warn') {{
          gCell = '🟡 Google ⚠️';
        }} else if (gBadge === 'bad') {{
          gCell = '🔴 Google ❌';
        }}
        const gHint = gDev == null ? '' : ' <span style="color:#6b7280;font-size:.75rem;">(~' + gDev.toFixed(1) + '%)</span>';
        const mid = sneakerModelId(it.brand, it.model);
        const rec = String(it.recommandation || '👍 BON PRIX');
        const pos = String(it.position_client || 'Milieu de gamme');
        const recCls = recBadgeClass(rec);
        const posCls = posBadgeClass(pos);
        return `<tr>
          <td>${{it.brand}} ${{it.model}}</td>
          <td>${{it.shop}} <span style="color:#6b7280;font-size:.77rem;">[${{it.source_status}}]</span></td>
          <td class="cmp-price"><span class="cmp-price-inner">${{it.price_min.toFixed(2)}}${{String.fromCharCode(0xA0)}}€</span></td>
          <td class="cmp-price"><span class="cmp-price-inner">${{it.price_avg.toFixed(2)}}${{String.fromCharCode(0xA0)}}€</span></td>
          <td class="cmp-price"><span class="cmp-price-inner">${{it.price_max.toFixed(2)}}${{String.fromCharCode(0xA0)}}€</span></td>
          <td>${{score}}/100</td>
          <td><span class="pill ${{c}}">${{it.credibility}}</span><span style="color:#6b7280;font-size:.75rem;">${{reason}}</span></td>
          <td>${{gCell}}${{gHint}}</td>
          <td><span class="${{recCls}}" title="${{escAttr(rec)}}">${{escAttr(rec)}}</span></td>
          <td><span class="${{posCls}}" title="${{escAttr(pos)}}">${{escAttr(pos)}}</span></td>
          <td style="cursor:default">
            <button type="button" class="btn-cmp-hist" data-mid="${{escAttr(mid)}}" title="Historique ~30 j.">Historique</button>
          </td>
        </tr>`;
      }}).join('');
    }}
    if (rows) {{
      rows.addEventListener('click', (ev) => {{
        const btn = ev.target.closest('button.btn-cmp-hist');
        if (!btn || !rows.contains(btn)) return;
        ev.preventDefault();
        ev.stopPropagation();
        const mid = btn.getAttribute('data-mid') || '';
        if (mid) cmpOpenHistory(mid);
      }});
    }}
    const histOv = document.getElementById('modal-history-overlay');
    if (histOv) histOv.addEventListener('click', (e) => {{ if (e.target === histOv) cmpCloseHistory(); }});

    fetch('/api/comparison/fr?' + qs.toString())
      .then(r => r.json())
      .then(data => {{
        const arr = data.items || [];
        if (arr.length) {{
          renderRows(arr, false);
          return;
        }}
        // Fallback UX: si strict filtre tout, on affiche le meilleur disponible.
        const qs2 = new URLSearchParams(qs.toString());
        qs2.set('validated_only', 'false');
        qs2.set('include_excluded', 'true');
        return fetch('/api/comparison/fr?' + qs2.toString())
          .then(r => r.json())
          .then(data2 => renderRows(data2.items || [], true));
      }})
      .catch(() => {{
        rows.innerHTML = '<tr><td colspan="11" style="color:#fca5a5">Erreur API comparaison.</td></tr>';
      }});
  </script>
</body></html>"""
    return HTMLResponse(content=html)


@app.get("/premium")
def premium_page():
    html = """
    <html>
    <head>
    <title>Premium Sneaker Intelligence</title>
    <style>
    body {
        background:#050b12;
        color:white;
        font-family:Arial;
        padding:40px;
        text-align:center;
    }
    h1 { color:#00ffd5; }
    .box {
        border:1px solid rgba(0,255,213,0.3);
        padding:25px;
        border-radius:10px;
        margin-top:30px;
        background:rgba(0,0,0,0.6);
    }
    .btn {
        display:inline-block;
        margin-top:20px;
        padding:15px 25px;
        background:#00ffd5;
        color:black;
        text-decoration:none;
        font-weight:bold;
        border-radius:8px;
    }
    </style>
    </head>

    <body>

    <h1>📡 Sneaker Intelligence Premium</h1>

    <div class="box">

    <p>✔ Accès aux meilleures opportunités sneakers</p>
    <p>✔ Prix réels (min / max / profit)</p>
    <p>✔ Mise à jour toutes les 2h</p>

    <h2>💰 Offre</h2>
    <p>1€ → Test 24h</p>
    <p>10€ → Accès complet mensuel</p>

    <h2>💳 Paiement Grey</h2>
    <p>Envoyez le paiement à :</p>

    <b>TON_EMAIL_GREY_ICI</b>

    <p>Ensuite envoyez une capture :</p>

    <b>TON_WHATSAPP_OU_TELEGRAM</b>

    <p>Activation en moins de 5 minutes</p>

    <a href="/search" class="btn">Retour à l'application</a>

    </div>

    </body>
    </html>
    """
    return HTMLResponse(content=html)


@app.get("/track/premium")
def track_premium() -> RedirectResponse:
    log_event("premium_clicks")
    return RedirectResponse(url="/premium", status_code=307)


@app.get("/analytics", response_class=HTMLResponse)
def analytics_page() -> HTMLResponse:
    stats = get_stats()
    views = int(stats.get("search_views", 0))
    clicks = int(stats.get("premium_clicks", 0))
    conversion = 0.0
    if views > 0:
        conversion = round((clicks / views) * 100, 2)
    scraper_rows = get_scraper_health_rows()
    up_rows = sum(1 for r in scraper_rows if bool(r.get("last_success")))
    total_rows = len(scraper_rows)
    coverage = round((up_rows / total_rows) * 100, 2) if total_rows else 0.0
    scraper_tbody = "".join(
        f"<tr>"
        f"<td>{escape(str(r.get('scraper_name') or ''))}</td>"
        f"<td>{'OK' if bool(r.get('last_success')) else 'KO'}</td>"
        f"<td>{int(r.get('nb_products') or 0)}</td>"
        f"<td>{escape(str(r.get('last_run') or ''))}</td>"
        f"<td>{escape(str(r.get('error_message') or ''))}</td>"
        f"</tr>"
        for r in scraper_rows
    ) or "<tr><td colspan='5' style='color:#94a3b8'>Aucune donnée de monitoring scraper.</td></tr>"
    html = f"""
<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Analytics — Sneaker Intelligence</title>
  <style>
    body {{
      margin: 0;
      min-height: 100vh;
      background: #0b0b0b;
      color: #f8fafc;
      font-family: ui-sans-serif, system-ui, Arial, sans-serif;
      padding: 2rem 1.25rem;
      display: flex;
      align-items: center;
      justify-content: center;
    }}
    .card {{
      max-width: 1100px;
      width: 100%;
      border: 1px solid #27272a;
      border-radius: 16px;
      padding: 1.75rem 1.5rem;
      background: linear-gradient(165deg, #111113 0%, #0a0a0c 100%);
      box-shadow: 0 20px 40px rgba(0,0,0,.45);
    }}
    h1 {{
      margin: 0 0 1.25rem;
      font-size: 1.35rem;
      font-weight: 800;
      letter-spacing: .04em;
      text-align: center;
      color: #fff;
    }}
    .row {{
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 1rem;
      margin: 0.85rem 0;
      padding-bottom: 0.75rem;
      border-bottom: 1px solid #27272a;
    }}
    .row:last-of-type {{ border-bottom: none; padding-bottom: 0; }}
    .label {{ color: #cbd5e1; font-size: 0.95rem; }}
    .value {{
      color: #22c55e;
      font-size: 1.35rem;
      font-weight: 800;
      font-variant-numeric: tabular-nums;
    }}
    .back {{
      display: inline-block;
      margin-top: 1.25rem;
      color: #00ffd5;
      text-decoration: none;
      font-weight: 600;
      font-size: 0.9rem;
    }}
    .back:hover {{ text-decoration: underline; }}
    .table-wrap {{ margin-top: 20px; overflow-x: auto; border: 1px solid #27272a; border-radius: 10px; }}
    table {{ width: 100%; border-collapse: collapse; min-width: 900px; }}
    th, td {{ text-align: left; padding: 10px 12px; border-bottom: 1px solid #1f2937; font-size: .9rem; }}
    th {{ color: #93c5fd; text-transform: uppercase; letter-spacing: .04em; font-size: .74rem; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>📊 ANALYTICS</h1>
    <div class="row">
      <span class="label">Visites</span>
      <span class="value">{views}</span>
    </div>
    <div class="row">
      <span class="label">Clics premium</span>
      <span class="value">{clicks}</span>
    </div>
    <div class="row">
      <span class="label">Conversion</span>
      <span class="value">{conversion}%</span>
    </div>
    <div class="row">
      <span class="label">Scrapers opérationnels</span>
      <span class="value">{up_rows}/{total_rows}</span>
    </div>
    <div class="row">
      <span class="label">Couverture scrapers</span>
      <span class="value">{coverage}%</span>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Scraper</th>
            <th>Status</th>
            <th>Nb produits</th>
            <th>Last run</th>
            <th>Error message</th>
          </tr>
        </thead>
        <tbody>
          {scraper_tbody}
        </tbody>
      </table>
    </div>
    <p style="margin:0;text-align:center;">
      <a class="back" href="/">← Retour</a>
    </p>
  </div>
</body>
</html>
"""
    return HTMLResponse(content=html)


def _load_supervisor_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    root_dir = Path(__file__).resolve().parent.parent
    for market in ("FR", "BE", "LU"):
        path = root_dir / "data" / f"market_{market.lower()}.csv"
        if not path.is_file():
            continue
        try:
            with path.open("r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    brand = str(row.get("brand") or "").strip()
                    model = str(row.get("model") or "").strip()
                    if not brand or not model:
                        continue
                    try:
                        pmin = float(row.get("price_min") or 0.0)
                        pmax = float(row.get("price_max") or 0.0)
                        pavg = float(row.get("price_avg") or 0.0)
                    except (TypeError, ValueError):
                        continue
                    rows.append(
                        {
                            "brand": brand,
                            "model": model,
                            "market": market,
                            "price_min": pmin,
                            "price_max": pmax,
                            "price_avg": pavg,
                        }
                    )
        except OSError:
            continue
    return rows


def _run_supervisor_job(job_id: str) -> None:
    supervisor = AISupervisor()
    rows = _load_supervisor_rows()
    total = len(rows)
    with _supervisor_lock:
        _supervisor_results["job_id"] = job_id
        _supervisor_results["status"] = "running"
        _supervisor_results["progress"] = f"0/{total}"
        _supervisor_results["done"] = 0
        _supervisor_results["total"] = total
        _supervisor_results["results"] = []

    for idx, row in enumerate(rows, start=1):
        analysis = supervisor.analyze_prices(
            brand=str(row["brand"]),
            model=str(row["model"]),
            market=str(row["market"]),
            price_min=float(row["price_min"]),
            price_max=float(row["price_max"]),
            price_avg=float(row["price_avg"]),
        )
        item = dict(row)
        item.update(analysis)
        with _supervisor_lock:
            current = list(_supervisor_results.get("results") or [])
            current.append(item)
            _supervisor_results["results"] = current
            _supervisor_results["done"] = idx
            _supervisor_results["progress"] = f"{idx}/{total}"

    with _supervisor_lock:
        _supervisor_results["status"] = "done"


@app.get("/supervisor", response_class=HTMLResponse)
def supervisor_page() -> HTMLResponse:
    html = """<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Superviseur IA</title>
  <style>
    body { margin:0; background:#0b0b0b; color:#fff; font-family:ui-sans-serif,system-ui,Arial,sans-serif; padding:24px; }
    .wrap { max-width:1200px; margin:0 auto; }
    .card { border:1px solid #1f2937; background:#101010; border-radius:14px; padding:18px; margin-bottom:16px; }
    h1 { margin:0 0 8px; color:#22c55e; letter-spacing:.03em; }
    .btn { display:inline-block; border:1px solid #22c55e; color:#dcfce7; background:#14532d; padding:10px 14px; border-radius:10px; font-weight:700; cursor:pointer; }
    .score { font-size:1.25rem; font-weight:800; color:#22c55e; margin-top:8px; }
    .progress-wrap { width:100%; height:14px; background:#1f2937; border-radius:999px; overflow:hidden; margin-top:10px; }
    .progress-bar { width:0%; height:100%; background:linear-gradient(90deg,#16a34a,#22c55e); transition:width .3s ease; }
    table { width:100%; border-collapse:collapse; }
    th,td { border-bottom:1px solid #1f2937; text-align:left; padding:10px 8px; vertical-align:top; }
    th { color:#a7f3d0; font-size:.88rem; letter-spacing:.04em; text-transform:uppercase; }
  </style>
</head>
<body>
  <main class="wrap">
    <section class="card">
      <h1>🤖 SUPERVISEUR IA</h1>
      <p style="margin:0 0 14px;color:#cbd5e1;">Detection des anomalies de prix sur FR/BE/LU.</p>
      <button class="btn" id="startBtn">Lancer l'analyse</button>
      <div id="statusTxt" style="margin-top:10px;color:#cbd5e1;">Statut: idle</div>
      <div id="progressTxt" style="margin-top:6px;color:#93c5fd;">Progression: 0/0</div>
      <div class="progress-wrap"><div class="progress-bar" id="progressBar"></div></div>
      <div class="score" id="scoreTxt">Score global de credibilite : 0%</div>
    </section>
    <section class="card">
      <table>
        <thead>
          <tr>
            <th>Marque</th>
            <th>Modele</th>
            <th>Marche</th>
            <th>Status</th>
            <th>Anomalies detectees</th>
          </tr>
        </thead>
        <tbody id="rowsBody">
          <tr><td colspan="5" style="padding:12px;color:#9ca3af;">Aucune analyse lancee.</td></tr>
        </tbody>
      </table>
    </section>
  </main>
  <script>
    let pollTimer = null;
    const statusTxt = document.getElementById("statusTxt");
    const progressTxt = document.getElementById("progressTxt");
    const scoreTxt = document.getElementById("scoreTxt");
    const rowsBody = document.getElementById("rowsBody");
    const progressBar = document.getElementById("progressBar");

    function badge(action) {
      const a = (action || "WARN").toUpperCase();
      if (a === "OK") return { text: "🟢 OK", color: "#22c55e" };
      if (a === "ALERT") return { text: "🔴 ALERT", color: "#ef4444" };
      return { text: "🟡 WARN", color: "#f59e0b" };
    }

    function renderRows(results) {
      if (!results || !results.length) {
        rowsBody.innerHTML = '<tr><td colspan="5" style="padding:12px;color:#9ca3af;">Aucune donnee.</td></tr>';
        return;
      }
      rowsBody.innerHTML = results.map((item) => {
        const b = badge(item.action);
        const anomalies = (item.anomalies || []).join(", ") || "-";
        return `<tr>
          <td>${item.brand || ""}</td>
          <td>${item.model || ""}</td>
          <td>${item.market || ""}</td>
          <td style="color:${b.color};font-weight:700;">${b.text}</td>
          <td>${anomalies}</td>
        </tr>`;
      }).join("");
    }

    function updateProgress(progress) {
      const parts = String(progress || "0/0").split("/");
      const done = Number(parts[0] || 0);
      const total = Number(parts[1] || 0);
      const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
      progressBar.style.width = `${pct}%`;
    }

    function updateScore(results) {
      const total = results.length;
      const ok = results.filter((r) => String(r.action || "").toUpperCase() === "OK").length;
      const score = total > 0 ? ((ok / total) * 100).toFixed(2) : "0.00";
      scoreTxt.textContent = `Score global de credibilite : ${score}%`;
    }

    async function refreshStatus() {
      const res = await fetch("/supervisor/status");
      const data = await res.json();
      statusTxt.textContent = `Statut: ${data.status}`;
      progressTxt.textContent = `Progression: ${data.progress}`;
      updateProgress(data.progress);
      renderRows(data.results || []);
      updateScore(data.results || []);
      if (data.status === "done" && pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
    }

    document.getElementById("startBtn").addEventListener("click", async () => {
      await fetch("/supervisor/start", { method: "POST" });
      if (pollTimer) clearInterval(pollTimer);
      await refreshStatus();
      pollTimer = setInterval(refreshStatus, 3000);
    });

    refreshStatus();
  </script>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.post("/supervisor/start")
def supervisor_start() -> dict[str, object]:
    global _supervisor_thread
    with _supervisor_lock:
        if _supervisor_results.get("status") == "running":
            return {
                "job_id": str(_supervisor_results.get("job_id") or ""),
                "status": "running",
            }
        job_id = str(int(time.time()))
        _supervisor_results["job_id"] = job_id
        _supervisor_results["status"] = "running"
        _supervisor_results["progress"] = "0/0"
        _supervisor_results["done"] = 0
        _supervisor_results["total"] = 0
        _supervisor_results["results"] = []
    _supervisor_thread = threading.Thread(target=_run_supervisor_job, args=(job_id,), daemon=True)
    _supervisor_thread.start()
    return {"job_id": job_id, "status": "running"}


@app.get("/supervisor/status")
def supervisor_status() -> dict[str, object]:
    with _supervisor_lock:
        return {
            "status": str(_supervisor_results.get("status") or "idle"),
            "progress": str(_supervisor_results.get("progress") or "0/0"),
            "results": list(_supervisor_results.get("results") or []),
        }


def _normalize_key(s: str) -> str:
    return " ".join((s or "").strip().lower().split())


def _build_model_aliases(model_in: str) -> list[str]:
    """
    Alias métier pour améliorer le matching UI -> market_live.csv.
    """
    base = _normalize_key(model_in)
    aliases = [base]
    alias_map: dict[str, list[str]] = {
        "air force 1": ["air force 1", "af1"],
        "air max 95": ["air max 95", "am95"],
    }
    for canonical, variants in alias_map.items():
        if canonical in base or any(v in base for v in variants):
            aliases.extend(variants)
    # dédupe en conservant l'ordre
    out: list[str] = []
    seen: set[str] = set()
    for a in aliases:
        aa = _normalize_key(a)
        if aa and aa not in seen:
            seen.add(aa)
            out.append(aa)
    return out


def _load_market_live_rows_full() -> list[dict[str, object]]:
    """
    Lit market_live.csv avec les champs nécessaires pour l'écran /search:
    produit, min, max, avg, trend.
    """
    from pathlib import Path
    import csv

    app_dir = Path(__file__).resolve().parent  # .../sneaker_bot/app
    root_dir = app_dir.parent  # .../sneaker_bot
    primary = app_dir / "data" / "market_live.csv"
    fallback = root_dir / "market_live.csv"
    path = primary if primary.is_file() else fallback
    if not path.is_file():
        return []

    try:
        with path.open(encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            fieldnames = set((reader.fieldnames or []))
            name_col = "product" if "product" in fieldnames else "produit" if "produit" in fieldnames else None
            if not name_col:
                return []
            needed = {"min", "max", "avg", "trend"}
            if not needed.issubset(fieldnames):
                return []

            rows: list[dict[str, object]] = []
            for r in reader:
                name = (r.get(name_col) or "").strip()
                if not name:
                    continue
                try:
                    rows.append(
                        {
                            "product": name,
                            "min": float(r.get("min") or 0.0),
                            "max": float(r.get("max") or 0.0),
                            "avg": float(r.get("avg") or 0.0),
                            "trend": str(r.get("trend") or "STABLE").strip().upper(),
                        }
                    )
                except (TypeError, ValueError):
                    continue
            return rows
    except OSError:
        return []


def _load_market_prices_rows(market: str) -> dict[tuple[str, str], dict[str, object]]:
    """
    Lit data/market_{market}.csv et prépare une lookup pour /search:
    (brand_norm, model_norm) -> {product, min, max, avg, trend}
    """
    from pathlib import Path
    import csv

    market_norm = "FR"

    app_dir = Path(__file__).resolve().parent  # .../sneaker_bot/app
    root_dir = app_dir.parent  # .../sneaker_bot
    path = root_dir / "data" / f"market_{market_norm.lower()}.csv"
    if not path.is_file():
        return {}

    out: dict[tuple[str, str], dict[str, object]] = {}
    try:
        with path.open(encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            fieldnames = set((reader.fieldnames or []))
            needed = {"brand", "model", "price_min", "price_max", "price_avg"}
            if not needed.issubset(fieldnames):
                return {}

            for r in reader:
                b = str(r.get("brand") or "").strip()
                m = str(r.get("model") or "").strip()
                if not b or not m:
                    continue

                try:
                    pmin = float(r.get("price_min") or 0.0)
                    pmax = float(r.get("price_max") or 0.0)
                    pavg = float(r.get("price_avg") or 0.0)
                except (TypeError, ValueError):
                    continue

                out[(_normalize_key(b), _normalize_key(m))] = {
                    "product": f"{b} {m}",
                    "min": pmin,
                    "max": pmax,
                    "avg": pavg,
                    "trend": "STABLE",
                }
    except OSError:
        return {}

    return out


def _render_search_page(
    *,
    brand: str,
    model: str,
    result: dict[str, object] | None,
    is_premium: bool,
    market: str,
    suggestions: list[dict[str, object]] | None = None,
) -> HTMLResponse:
    catalog = _load_models_catalog_from_json()
    brand_keys = list(catalog.keys())
    selected_brand = brand if brand in catalog else (brand_keys[0] if brand_keys else "")
    selected_models = catalog.get(selected_brand, [])
    selected_model = model if model in selected_models else (selected_models[0] if selected_models else "")
    selected_market = "FR"

    def _supervisor_badge(brand_value: str, model_value: str) -> tuple[str, str]:
        rows = list(_supervisor_results.get("results") or [])
        b_key = _normalize_key(brand_value)
        m_key = _normalize_key(model_value)
        for row in reversed(rows):
            if not isinstance(row, dict):
                continue
            if _normalize_key(str(row.get("brand") or "")) != b_key:
                continue
            if _normalize_key(str(row.get("model") or "")) != m_key:
                continue
            action = str(row.get("action") or "").upper()
            if action == "OK":
                return ("🟢 Prix vérifié", "badge-ok")
            if action == "ALERT":
                return ("🔴 Anomalie", "badge-alert")
            return ("🟡 À vérifier", "badge-warn")
        return ("🟡 À vérifier", "badge-warn")

    result_block = ""
    if result is not None:
        pmin = float(result.get("min") or 0.0)
        pmax = float(result.get("max") or 0.0)
        pavg = float(result.get("avg") or 0.0)
        min_scale = min(pmin, pavg, pmax)
        max_scale = max(pmin, pavg, pmax)
        scale_span = max(max_scale - min_scale, 1.0)
        min_left = ((pmin - min_scale) / scale_span) * 100
        avg_left = ((pavg - min_scale) / scale_span) * 100
        max_left = ((pmax - min_scale) / scale_span) * 100
        good_deal_badge = '<span class="badge-good">🔥 Bonne affaire</span>' if pmin < pavg * 0.7 else ""
        high_badge = '<span class="badge-high">⚠️ Prix élevé</span>' if pmax > pavg * 1.8 else ""
        cards_html = f"""
        <div class="market-card fade winner">
          <div class="market-head"><h3>FR</h3><span class="badge-cheap">🇫🇷 Marché France</span></div>
          <p>MIN: <b>{pmin:.0f}\u00a0€</b></p>
          <p>MAX: <b>{pmax:.0f}\u00a0€</b></p>
          <p>MOY: <b>{pavg:.0f}\u00a0€</b></p>
          <div class="price-bar">
            <div class="track"></div>
            <span class="dot min" style="left:{min_left:.1f}%;">MIN</span>
            <span class="dot avg" style="left:{avg_left:.1f}%;">MOY</span>
            <span class="dot max" style="left:{max_left:.1f}%;">MAX</span>
          </div>
          <div class="card-badges">{good_deal_badge}{high_badge}</div>
        </div>
        """
        best_buy_market = "FR"
        best_sell_market = "FR"
        best_buy = pmin
        best_sell = pmax
        profit = best_sell - best_buy
        reco = "✅ Opportunité" if profit > 30 else "📊 Marché stable"
        badge_text, badge_class = _supervisor_badge(brand, model)
        product_label = f"{brand} {model}".strip() or str(result.get("product") or "")
        image_slug = quote(f"{brand}-{model}".strip().replace(" ", "-"))
        image_url = f"https://duckduckgo.com/i/{image_slug}.jpg"

        result_block = f"""
        <section class="product-shell">
          <div class="product-hero fade">
            <div class="hero-media">
              <img src="{escape(image_url)}" alt="{escape(product_label)}"
                   onerror="this.style.display='none';this.nextElementSibling.style.display='flex';" />
              <div class="img-fallback">👟</div>
            </div>
            <div class="hero-content">
              <h2>{escape(product_label)}</h2>
              <p class="cred {badge_class}">{badge_text}</p>
            </div>
          </div>
          <div class="market-grid">{cards_html}</div>
          <div class="analysis fade">
            <h3>📊 ANALYSE DE MARCHÉ</h3>
            <p>Acheter à <b>{best_buy:.0f}\u00a0€</b> sur <b>{best_buy_market}</b></p>
            <p>Meilleur prix de revente estimé: <b>{best_sell:.0f}\u00a0€</b> ({best_sell_market})</p>
            <p>Profit potentiel: <b>{profit:.0f}\u00a0€</b></p>
            <p class="reco">{reco}</p>
          </div>
        </section>
        """
    elif brand and model:
        suggestion_rows = suggestions or []
        if suggestion_rows:
            items_html = "".join(
                f"""
                <li style="margin:10px 0;padding:10px;border:1px solid #2a2a2a;border-radius:10px;background:rgba(255,255,255,.02);">
                  <strong style="color:#fff;">{escape(str(s.get('product') or 'N/A'))}</strong><br>
                  <span style="color:#cbd5e1;">💰 Min: {float(s.get('min') or 0.0):.0f}\u00a0€ · Max: {float(s.get('max') or 0.0):.0f}\u00a0€ · Avg: {float(s.get('avg') or 0.0):.0f}\u00a0€</span>
                </li>
                """
                for s in suggestion_rows[:3]
            )
            result_block = f"""
            <div style="margin-top:16px;border:1px solid #232323;border-radius:16px;padding:16px;background:rgba(15,15,15,.35);">
              <h2 style="margin:0 0 10px;color:#fff;">Produit non trouvé — suggestions proches :</h2>
              <ul style="list-style:none;padding:0;margin:0;">{items_html}</ul>
            </div>
            """
        else:
            result_block = """
            <div style="margin-top:16px;border:1px solid #232323;border-radius:16px;padding:16px;background:rgba(15,15,15,.35);">
              <h2 style="margin:0 0 10px;color:#fff;">Aucune donnée disponible pour ce modèle</h2>
              <p style="margin:0;color:#e5e7eb;">Essayez un autre modèle ou relancez le scraping FR.</p>
            </div>
            """

    brand_options = "".join(
        f"<option value='{escape(b)}' {'selected' if b == selected_brand else ''}>{escape(b)}</option>"
        for b in catalog.keys()
    )
    model_options = "".join(
        f"<option value='{escape(m)}' {'selected' if m == selected_model else ''}>{escape(m)}</option>"
        for m in selected_models
    )

    js_catalog = json.dumps(catalog, ensure_ascii=False)

    html = f"""
<html>
<body style="background:#0b0b0b;color:white;font-family:sans-serif;padding:20px;max-width:1100px;margin:0 auto;">
  <style>
    @keyframes fadeInUp {{
      from {{ opacity: 0; transform: translateY(10px); }}
      to {{ opacity: 1; transform: translateY(0); }}
    }}
    .fade {{ animation: fadeInUp .45s ease both; }}
    .product-shell {{ margin-top:20px; }}
    .product-hero {{ display:flex; gap:16px; align-items:center; background:#111; border:1px solid #1f1f1f; border-radius:16px; padding:16px; }}
    .hero-media {{ width:120px; height:120px; border-radius:12px; overflow:hidden; background:#0f0f0f; border:1px solid #2a2a2a; flex-shrink:0; display:flex; align-items:center; justify-content:center; }}
    .hero-media img {{ width:100%; height:100%; object-fit:cover; }}
    .img-fallback {{ display:none; width:100%; height:100%; align-items:center; justify-content:center; font-size:2.6rem; }}
    .hero-content h2 {{ margin:0; font-size:2rem; font-weight:800; color:#f8fafc; }}
    .cred {{ margin-top:8px; display:inline-block; padding:6px 10px; border-radius:999px; font-size:.85rem; }}
    .badge-ok {{ background:rgba(34,197,94,.15); border:1px solid rgba(34,197,94,.4); color:#86efac; }}
    .badge-warn {{ background:rgba(245,158,11,.15); border:1px solid rgba(245,158,11,.4); color:#fcd34d; }}
    .badge-alert {{ background:rgba(239,68,68,.15); border:1px solid rgba(239,68,68,.4); color:#fca5a5; }}
    .market-grid {{ margin-top:16px; display:grid; gap:12px; grid-template-columns:1fr; }}
    .market-card {{ background:#121212; border:1px solid #2b2b2b; border-radius:14px; padding:14px; transition:border-color .2s ease, transform .2s ease; }}
    .market-card:hover {{ border-color:#22c55e; transform:translateY(-2px); }}
    .market-card.winner {{ border-color:#22c55e; box-shadow:0 0 0 1px rgba(34,197,94,.2) inset; }}
    .market-head {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:8px; }}
    .market-head h3 {{ margin:0; color:#fff; }}
    .missing {{ color:#94a3b8; margin:8px 0 0; }}
    .badge-cheap {{ font-size:.75rem; background:#14532d; color:#bbf7d0; border:1px solid rgba(34,197,94,.45); border-radius:999px; padding:4px 8px; }}
    .price-bar {{ position:relative; height:34px; margin:10px 0 8px; }}
    .track {{ position:absolute; left:0; right:0; top:15px; height:4px; background:#1f2937; border-radius:999px; }}
    .dot {{ position:absolute; top:2px; transform:translateX(-50%); font-size:.67rem; font-weight:700; padding:2px 5px; border-radius:7px; }}
    .dot.min {{ background:#1d4ed8; color:#dbeafe; }}
    .dot.avg {{ background:#15803d; color:#dcfce7; }}
    .dot.max {{ background:#b91c1c; color:#fee2e2; }}
    .card-badges {{ display:flex; gap:6px; flex-wrap:wrap; min-height:24px; }}
    .badge-good {{ font-size:.72rem; background:#064e3b; color:#a7f3d0; border:1px solid rgba(16,185,129,.45); border-radius:999px; padding:4px 8px; }}
    .badge-high {{ font-size:.72rem; background:#4c0519; color:#fda4af; border:1px solid rgba(244,63,94,.45); border-radius:999px; padding:4px 8px; }}
    .analysis {{ margin-top:16px; border:1px solid #2a2a2a; border-radius:16px; background:linear-gradient(160deg, rgba(17,24,39,.65), rgba(2,6,23,.5)); padding:16px; }}
    .analysis h3 {{ margin:0 0 8px; }}
    .analysis p {{ margin:7px 0; color:#d1d5db; }}
    .analysis .reco {{ margin-top:10px; font-weight:800; color:#22c55e; }}
    .back-btn {{ display:inline-block; margin-top:18px; color:#22c55e; text-decoration:none; border:1px solid #22c55e55; border-radius:10px; padding:9px 14px; }}
    @media (max-width: 900px) {{ .market-grid {{ grid-template-columns:1fr; }} .product-hero {{ flex-direction:column; align-items:flex-start; }} }}
  </style>
  <h1 style="margin:0 0 10px;">🔎 Recherche Sneaker (Marché France)</h1>
  <p style="margin:0;color:#cbd5e1;font-size:.95rem;">Choisissez une marque et un modèle, puis analysez.</p>

  <form method="get" action="/search" style="margin-top:16px;display:grid;grid-template-columns:1fr 1fr auto;gap:10px;align-items:center;">
    <label style="display:block;">
      <span style="display:block;margin-bottom:6px;color:#94a3b8;font-size:.8rem;">MARQUE</span>
      <select name="brand" id="brandSelect" style="width:100%;padding:10px;border-radius:10px;border:1px solid #2d2d2d;background:#0f0f0f;color:#fff;">
        {brand_options}
      </select>
    </label>
    <label style="display:block;">
      <span style="display:block;margin-bottom:6px;color:#94a3b8;font-size:.8rem;">MODÈLE</span>
      <select name="model" id="modelSelect" style="width:100%;padding:10px;border-radius:10px;border:1px solid #2d2d2d;background:#0f0f0f;color:#fff;">
        {model_options}
      </select>
    </label>
    <button type="submit" style="padding:12px 16px;border-radius:12px;border:0;background:#00c896;color:#052e16;font-weight:800;cursor:pointer;">
      Analyser
    </button>
  </form>

  {result_block}

  <a href="/" class="back-btn">← Retour</a>

  <script>
    const catalog = {js_catalog};
    const brandSelect = document.getElementById("brandSelect");
    const modelSelect = document.getElementById("modelSelect");

    function refreshModels() {{
      const brand = brandSelect.value;
      const models = catalog[brand] || [];
      modelSelect.innerHTML = "";

      if (!models.length) {{
        const opt = document.createElement("option");
        opt.value = "";
        opt.textContent = "No model";
        modelSelect.appendChild(opt);
        return;
      }}

      models.forEach(m => {{
        const opt = document.createElement("option");
        opt.value = m;
        opt.textContent = m;
        modelSelect.appendChild(opt);
      }});
    }}

    brandSelect.addEventListener("change", refreshModels);
  </script>
</body>
</html>
"""
    return HTMLResponse(html)


def _search_result(brand_in: str, model_in: str) -> dict[str, object] | None:
    brand_norm = _normalize_key(brand_in)
    model_norm = _normalize_key(model_in)
    aliases = _build_model_aliases(model_in)
    rows = _load_market_live_rows_full()
    if not rows:
        return None

    # 1) Match alias explicite (brand + any(alias in product))
    alias_candidates: list[tuple[int, dict[str, object]]] = []
    for r in rows:
        product_norm = _normalize_key(str(r.get("product") or ""))
        if brand_norm not in product_norm:
            continue
        matched_alias_len = 0
        for a in aliases:
            if a in product_norm:
                matched_alias_len = max(matched_alias_len, len(a))
        if matched_alias_len > 0:
            # score priorise alias le plus précis + proximité longueur
            score = matched_alias_len * 10 - abs(len(product_norm) - len(f"{brand_norm} {model_norm}"))
            alias_candidates.append((score, r))
    if alias_candidates:
        alias_candidates.sort(key=lambda x: x[0], reverse=True)
        return alias_candidates[0][1]

    # 2) Fallback partiel: brand + tokens modèle
    token_candidates: list[tuple[int, dict[str, object]]] = []
    tokens = [t for t in model_norm.split() if t]
    for r in rows:
        product_norm = _normalize_key(str(r.get("product") or ""))
        if brand_norm not in product_norm:
            continue
        token_hits = sum(1 for t in tokens if t in product_norm)
        if token_hits <= 0:
            continue
        score = token_hits * 10 - abs(len(product_norm) - len(f"{brand_norm} {model_norm}"))
        token_candidates.append((score, r))
    if token_candidates:
        token_candidates.sort(key=lambda x: x[0], reverse=True)
        return token_candidates[0][1]

    # 3) Dernier fallback: brand uniquement (renvoie le meilleur dispo)
    brand_candidates = [
        r for r in rows if brand_norm in _normalize_key(str(r.get("product") or ""))
    ]
    if brand_candidates:
        brand_candidates.sort(key=lambda r: abs(len(_normalize_key(str(r.get("product") or ""))) - len(brand_norm)))
        return brand_candidates[0]

    return None


def _search_with_fallback(
    brand_in: str,
    model_in: str,
) -> tuple[dict[str, object] | None, list[dict[str, object]]]:
    rows = _load_market_live_rows_full()
    if not brand_in or not model_in:
        return None, []

    key_exact = _normalize_key(f"{brand_in} {model_in}")
    brand_norm = _normalize_key(brand_in)
    model_norm = _normalize_key(model_in)

    same_brand_rows = [
        r
        for r in rows
        if _normalize_key(str(r.get("product") or "")).startswith(f"{brand_norm} ")
    ]

    # 1) Match exact
    for r in same_brand_rows:
        if _normalize_key(str(r.get("product") or "")) == key_exact:
            return r, []

    # 2) Match partiel "contains"
    for r in same_brand_rows:
        prod_norm = _normalize_key(str(r.get("product") or ""))
        if model_norm and model_norm in prod_norm:
            return r, []

    # 3) Suggestions top 3 de la même marque
    def _score(row: dict[str, object]) -> int:
        prod_norm = _normalize_key(str(row.get("product") or ""))
        score = 0
        for w in model_norm.split():
            if w and w in prod_norm:
                score += len(w)
        return score

    ranked = sorted(same_brand_rows, key=_score, reverse=True)
    suggestions = ranked[:3]
    return None, suggestions


@app.get("/search", response_class=HTMLResponse)
def search(request: Request, brand: str = "", model: str = "", market: str = "FR"):
    log_event("search_views")
    is_premium = False
    market_norm = "FR"
    # Normaliser les entrées utilisateur
    brand_in = (brand or "").strip()
    model_in = (model or "").strip()

    catalog_ui = _load_models_catalog_from_json()
    # Validation rapide : brand + model doivent exister dans models_list.json.
    if brand_in not in catalog_ui:
        return _render_search_page(
            brand=brand_in,
            model=model_in,
            result=None,
            is_premium=is_premium,
            market=market_norm,
            suggestions=[],
        )
    if not model_in:
        return _render_search_page(
            brand=brand_in,
            model=model_in,
            result=None,
            is_premium=is_premium,
            market=market_norm,
            suggestions=[],
        )

    # Keep selected model in sync with dropdown values.
    selected_models = catalog_ui.get(brand_in, [])
    if model_in not in selected_models and selected_models:
        model_in = selected_models[0]

    key = (_normalize_key(brand_in), _normalize_key(model_in))
    lookup = _load_market_prices_rows("FR")
    found = lookup.get(key)
    suggestions: list[dict[str, object]] = []
    if found is None:
        # 2) Fallback to legacy market_live matching
        found, suggestions = _search_with_fallback(brand_in, model_in)
    return _render_search_page(
        brand=brand_in,
        model=model_in,
        result=found,
        is_premium=is_premium,
        market=market_norm,
        suggestions=suggestions,
    )


@app.post("/search", response_class=HTMLResponse)
def search_post(
    brand: str = Form(""),
    model: str = Form(""),
    market: str = Form("FR"),
):
    is_premium = False
    market_norm = "FR"
    brand_in = (brand or "").strip()
    model_in = (model or "").strip()

    catalog_ui = _load_models_catalog_from_json()
    # Validation rapide : brand + model doivent exister dans models_list.json.
    if brand_in not in catalog_ui:
        return _render_search_page(
            brand=brand_in,
            model=model_in,
            result=None,
            is_premium=is_premium,
            market=market_norm,
            suggestions=[],
        )
    if not model_in:
        return _render_search_page(
            brand=brand_in,
            model=model_in,
            result=None,
            is_premium=is_premium,
            market=market_norm,
            suggestions=[],
        )

    selected_models = catalog_ui.get(brand_in, [])
    if model_in not in selected_models and selected_models:
        model_in = selected_models[0]

    key = (_normalize_key(brand_in), _normalize_key(model_in))
    lookup = _load_market_prices_rows("FR")
    found = lookup.get(key)
    suggestions = []
    if found is None:
        found, suggestions = _search_with_fallback(brand_in, model_in)
    return _render_search_page(
        brand=brand_in,
        model=model_in,
        result=found,
        is_premium=is_premium,
        market=market_norm,
        suggestions=suggestions,
    )
