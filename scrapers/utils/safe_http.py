"""Requêtes HTTP « safe » — retry simple + headers variables (couche additive)."""

from __future__ import annotations

import time
from typing import Any

import requests

from scrapers.utils.rate_limiter import global_rate_limiter


def get_safe_headers() -> dict[str, str]:
    import random as _r

    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/121 Safari/537.36",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X)",
        "Mozilla/5.0 (Linux; Android 11)",
    ]
    return {
        "User-Agent": _r.choice(user_agents),
        "Accept-Language": "fr-FR,fr;q=0.9",
        "Referer": "https://www.google.com/",
    }


def safe_request(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: float = 10,
    retries: int = 3,
) -> requests.Response | None:
    """GET avec backoff ; succès = HTTP 200 et corps > 100 caractères."""
    h = dict(headers) if headers else {}
    n = max(1, int(retries))
    for i in range(n):
        try:
            global_rate_limiter.wait()
            r = requests.get(url, headers=h, timeout=timeout)
            if r.status_code == 200 and len(r.text or "") > 100:
                return r
        except Exception:
            pass
        time.sleep(1.5 * (i + 1))
    return None
