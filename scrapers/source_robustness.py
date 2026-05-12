"""
Couche de normalisation / robustesse pour les entrées « source » (collecte → agrégation).

Fonctions pures (hors I/O). Ne modifie pas les schémas CSV publics : les colonnes
``price_min``, ``price_max``, ``price_avg``, ``updated_at``, etc. restent les mêmes.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from scrapers.utils.normalize import EUR_RATES, parse_price_to_eur

# Seuil « prix proche » pour dédoublonnage (même boutique)
DEFAULT_PRICE_EPSILON_EUR = 2.0

# Données considérées périmées au-delà de cette fenêtre (logs / logique de repli)
STALE_HOURS = 24.0


def normalize_price(value: Any) -> float | None:
    """
    Normalisation centrale des prix.

    Règles:
    - accepte ``str``/numérique
    - retire symbole euro, espaces (dont insécables) et séparateurs de milliers
    - remplace la virgule décimale par un point
    - retourne ``None`` si non numérique, NaN/inf, ``<= 0`` ou ``> 5000``
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
    else:
        s = str(value).strip()
        if not s:
            return None
        s = (
            s.replace("€", "")
            .replace("\u00a0", "")
            .replace(" ", "")
            .replace("\t", "")
            .replace(",", ".")
        )
        # Cas "1.299.99" -> retire les points de milliers (garde le dernier décimal).
        if s.count(".") > 1:
            head, tail = s.rsplit(".", 1)
            s = head.replace(".", "") + "." + tail
        try:
            v = float(s)
        except (TypeError, ValueError):
            return None
    if math.isnan(v) or math.isinf(v):
        return None
    if v <= 0 or v > 5000:
        return None
    return round(v, 2)


def is_valid_price(x: Any) -> bool:
    """Prix valide = normalisable et dans un intervalle raisonnable."""
    return normalize_price(x) is not None


def scrub_price_list(prices: list[float] | list[Any]) -> list[float]:
    """Conserve uniquement les prix normalisés valides."""
    out: list[float] = []
    for p in prices:
        v = normalize_price(p)
        if v is None:
            continue
        out.append(v)
    return out


def convert_to_eur(amount: Any, currency: str = "EUR") -> float | None:
    """
    Convertit un montant numérique vers EUR via les taux connus.
    Pour les chaînes ambiguës, déléguer à ``parse_price_to_eur``.
    """
    if isinstance(amount, str):
        return parse_price_to_eur(amount, currency_hint=currency)
    if not is_valid_price(amount):
        return None
    cur = (currency or "EUR").strip().upper()
    rate = float(EUR_RATES.get(cur, 1.0))
    return round(float(amount) * rate, 2)


def normalize_source_entry(
    *,
    price: Any,
    currency: str = "EUR",
    source: str = "",
    url: str = "",
    timestamp: Any = None,
) -> dict[str, Any] | None:
    """
    Normalise une entrée source en dict canonique
    ``{price, currency, source, url, timestamp}`` (prix toujours en EUR si convertible).
    Retourne None si prix invalide.
    """
    cur = (currency or "EUR").strip().upper() or "EUR"
    eur: float | None
    if isinstance(price, str):
        eur = parse_price_to_eur(price, currency_hint=cur)
    else:
        eur = convert_to_eur(price, cur)
    if eur is None or not is_valid_price(eur):
        return None
    ts = "" if timestamp is None else str(timestamp).strip()
    return {
        "price": round(float(eur), 2),
        "currency": "EUR",
        "source": (source or "").strip(),
        "url": (url or "").strip(),
        "timestamp": ts,
    }


def _parse_csv_datetime(ts: str) -> datetime | None:
    s = (ts or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def hours_since_row_timestamp(updated_at: str, *, now: datetime | None = None) -> float | None:
    """Âge en heures d’une valeur ``updated_at`` CSV ; None si parsing impossible."""
    dt = _parse_csv_datetime(updated_at)
    if dt is None:
        return None
    ref = now or datetime.now()
    return max(0.0, (ref - dt).total_seconds() / 3600.0)


def is_stale_csv_timestamp(updated_at: str, *, now: datetime | None = None, max_hours: float = STALE_HOURS) -> bool:
    h = hours_since_row_timestamp(updated_at, now=now)
    if h is None:
        return False
    return h > max_hours


def price_bucket_eur(price_avg: float, epsilon: float = DEFAULT_PRICE_EPSILON_EUR) -> int:
    if epsilon <= 0:
        epsilon = DEFAULT_PRICE_EPSILON_EUR
    return int(round(float(price_avg) / epsilon))


def dedupe_source_csv_rows(
    rows: list[dict[str, Any]],
    *,
    price_epsilon: float = DEFAULT_PRICE_EPSILON_EUR,
) -> list[dict[str, Any]]:
    """
    Supprime les doublons : même marque/modèle/boutique et prix moyen dans le même
    bucket (epsilon €), ou même URL si renseignée.
    En cas de doublon, conserve la ligne avec le ``price_count`` le plus élevé,
    puis la ``updated_at`` la plus récente.
    """
    if not rows:
        return []

    def sort_key(r: dict[str, Any]) -> tuple[Any, ...]:
        try:
            pc = int(float(str(r.get("price_count") or "0").replace(",", ".")))
        except (TypeError, ValueError):
            pc = 0
        return (pc, str(r.get("updated_at") or ""))

    by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    for r in rows:
        b = str(r.get("brand") or "").strip().lower()
        m = str(r.get("model") or "").strip().lower()
        shop = str(r.get("shop") or "").strip().lower()
        url = str(r.get("url") or "").strip().lower()
        try:
            pa = float(str(r.get("price_avg") or "").replace(",", "."))
        except (TypeError, ValueError):
            continue
        if not is_valid_price(pa):
            continue

        if url:
            dedupe_key: tuple[Any, ...] = ("url", b, m, url)
        else:
            dedupe_key = ("shop_price", b, m, shop, price_bucket_eur(pa, price_epsilon))

        prev = by_key.get(dedupe_key)
        if prev is None or sort_key(r) > sort_key(prev):
            by_key[dedupe_key] = dict(r)

    return list(by_key.values())


def aggregate_stats_from_prices(prices: list[float]) -> dict[str, float | int] | None:
    """min, max, median sur une liste de prix déjà validés."""
    vals = sorted(scrub_price_list(prices))
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    if n % 2 == 1:
        med = float(vals[mid])
    else:
        med = (float(vals[mid - 1]) + float(vals[mid])) / 2.0
    return {
        "price_min": float(vals[0]),
        "price_max": float(vals[-1]),
        "price_median": round(med, 2),
        "sources_count": n,
    }
