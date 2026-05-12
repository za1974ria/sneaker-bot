"""HTTP GET avec tentatives multiples — ne lève jamais d’exception (retour ``None`` si échec)."""

from __future__ import annotations

import logging
import random
import time
from typing import Any

import requests

from scrapers.utils.rate_limiter import global_rate_limiter

logger = logging.getLogger(__name__)

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _prepare_request_kwargs(session: requests.Session, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Ajoute un User-Agent réaliste seulement si aucun n’est déjà fourni (headers ou session)."""
    req = dict(kwargs)
    if "headers" in req:
        h = req["headers"]
        if h is not None:
            hd = dict(h)
            if not any(str(k).lower() == "user-agent" for k in hd):
                hd["User-Agent"] = _DEFAULT_UA
            req["headers"] = hd
    else:
        sess_ua = str(
            session.headers.get("User-Agent")
            or session.headers.get("user-agent")
            or ""
        ).strip()
        if not sess_ua:
            req["headers"] = {"User-Agent": _DEFAULT_UA}
    return req


def fetch_with_retry(
    session: requests.Session,
    url: str,
    *,
    timeout: float = 10.0,
    attempts: int = 3,
    min_body_chars: int = 0,
    require_status_ok: bool = False,
    **kwargs: Any,
) -> requests.Response | None:
    """
    Jusqu'à ``attempts`` GET ; délai aléatoire avant chaque tentative ; timeout fixe.
    Retourne ``None`` si toutes les tentatives échouent (aucune exception levée).

    Si ``require_status_ok`` ou ``min_body_chars`` > 0, les réponses insuffisantes
    déclenchent une nouvelle tentative (comportement inchangé si les deux sont neutres).
    """
    n = max(1, int(attempts))
    req_kwargs = _prepare_request_kwargs(session, kwargs)
    min_body = max(0, int(min_body_chars))
    for i in range(n):
        try:
            time.sleep(random.uniform(0.5, 1.5))
            global_rate_limiter.wait()
            resp = session.get(url, timeout=timeout, **req_kwargs)
            ok = True
            if require_status_ok and getattr(resp, "status_code", 0) != 200:
                ok = False
            elif min_body > 0 and len(resp.text or "") < min_body:
                ok = False
            if not ok:
                logger.debug(
                    "fetch_with_retry %s réponse rejetée (status=%s len=%s) tentative %d/%d",
                    url,
                    getattr(resp, "status_code", None),
                    len(resp.text or "") if resp is not None else 0,
                    i + 1,
                    n,
                )
                if i + 1 < n:
                    time.sleep(0.25 * (i + 1))
                continue
            return resp
        except (requests.RequestException, OSError) as e:
            logger.debug(
                "fetch_with_retry %s tentative %d/%d échouée: %s",
                url,
                i + 1,
                n,
                e,
            )
            if i + 1 < n:
                time.sleep(0.25 * (i + 1))
    return None


def fetch_url_get(
    url: str,
    *,
    timeout: float = 10.0,
    attempts: int = 3,
    **kwargs: Any,
) -> requests.Response | None:
    """GET avec une ``requests.Session()`` locale (aucune session réutilisable côté appelant)."""
    return fetch_with_retry(requests.Session(), url, timeout=timeout, attempts=attempts, **kwargs)
