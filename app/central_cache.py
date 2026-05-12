"""
Cache central optionnel (Redis) avec repli mémoire process-local.

Variables d'environnement:
- REDIS_URL : URL redis://... (optionnel)
- CACHE_TTL_SEC : TTL par défaut (défaut 600)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

_PREFIX = "sb:v1:"
_DEFAULT_TTL = int((os.getenv("CACHE_TTL_SEC") or os.getenv("REDIS_CACHE_TTL_SEC") or "600").strip() or "600")

_local_lock = threading.Lock()
_local_store: dict[str, tuple[float, int, str]] = {}  # key -> (stored_at, ttl_sec, json_str)

_redis = None
_redis_available = False


def _connect_redis() -> None:
    global _redis, _redis_available
    url = (os.getenv("REDIS_URL") or "").strip()
    if not url:
        return
    try:
        import redis as redis_lib  # type: ignore[import-untyped]

        client = redis_lib.Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=2.5,
            socket_timeout=2.5,
        )
        client.ping()
        _redis = client
        _redis_available = True
        logger.info("Cache central: Redis connecté")
    except Exception as exc:  # noqa: BLE001
        _redis = None
        _redis_available = False
        logger.warning("Cache central: Redis indisponible (%s), repli mémoire", exc)


def init_central_cache() -> None:
    """À appeler une fois au démarrage (idempotent)."""
    _connect_redis()


def redis_ready() -> bool:
    return bool(_redis_available and _redis is not None)


def default_ttl_sec() -> int:
    return max(60, _DEFAULT_TTL)


def _k(key: str) -> str:
    return key if key.startswith(_PREFIX) else f"{_PREFIX}{key}"


def get_json(key: str) -> Any | None:
    """Retourne la valeur désérialisée si présente et non expirée."""
    full = _k(key)
    if _redis is not None:
        try:
            raw = _redis.get(full)
            if raw:
                return json.loads(raw)
        except Exception:  # noqa: BLE001
            pass
    now = time.time()
    with _local_lock:
        entry = _local_store.get(full)
        if not entry:
            return None
        stored_at, ttl_sec, raw = entry
        if (now - stored_at) >= float(ttl_sec):
            del _local_store[full]
            return None
        try:
            return json.loads(raw)
        except Exception:  # noqa: BLE001
            return None


def set_json(key: str, value: Any, ttl_sec: int | None = None) -> None:
    ttl = int(ttl_sec if ttl_sec is not None else default_ttl_sec())
    ttl = max(30, ttl)
    full = _k(key)
    raw = json.dumps(value, ensure_ascii=False, default=str)
    if _redis is not None:
        try:
            _redis.setex(full, ttl, raw)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Redis setex %s: %s", full, exc)
    with _local_lock:
        _local_store[full] = (time.time(), ttl, raw)


def delete_key(key: str) -> None:
    full = _k(key)
    if _redis is not None:
        try:
            _redis.delete(full)
        except Exception:  # noqa: BLE001
            pass
    with _local_lock:
        _local_store.pop(full, None)


def delete_prefix(prefix: str) -> None:
    """Supprime toutes les clés dont le préfixe correspond (namespace sb:v1)."""
    p = _k(prefix) if not prefix.startswith(_PREFIX) else prefix
    if _redis is not None:
        try:
            for k in _redis.scan_iter(match=f"{p}*", count=256):
                try:
                    _redis.delete(k)
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            logger.debug("Redis delete_prefix: %s", exc)
    with _local_lock:
        to_del = [k for k in _local_store if k.startswith(p)]
        for k in to_del:
            _local_store.pop(k, None)


def invalidate_dashboard_keys() -> None:
    """Clés utilisées par analytics, health, fraîcheur, tracker."""
    for suffix in (
        "analytics_snapshot",
        "health_global",
        "data_freshness",
        "tracker:stats",
        "tracker:health",
    ):
        delete_key(suffix)
