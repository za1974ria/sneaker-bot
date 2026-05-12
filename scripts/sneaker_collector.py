"""
Collecteur de données sneakers (Vinted, Leboncoin, 2ememain).

Les pages listées sont souvent rendues en JavaScript ou protégées : le parsing HTML
peut retourner 0 résultat tant que les sélecteurs ne sont pas adaptés au DOM actuel.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

MIN_PRICE_EUR = 20.0
MAX_PRICE_EUR = 1000.0

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}

SESSION = requests.Session()
SESSION.headers.update(DEFAULT_HEADERS)

# Reconnaissance de prix en euros dans un texte (ex. "89,00 €", "120 €")
_PRICE_EUR_RE = re.compile(
    r"(?<![\d])(\d{1,4}(?:[.,]\d{1,2})?)\s*(?:€|EUR|euros?)(?![\d])",
    re.IGNORECASE,
)


def _parse_price_eur(raw: str | float | int | None) -> float | None:
    """Convertit une chaîne ou nombre en prix float €."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip().replace("\xa0", " ").replace(" ", "")
    s = s.replace(",", ".")
    m = re.search(r"(\d{1,4}(?:\.\d{1,2})?)", s)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _price_ok(price: float) -> bool:
    return MIN_PRICE_EUR <= price <= MAX_PRICE_EUR


def _append_if_ok(
    out: list[dict[str, Any]], name: str | None, price: float | None, source: str
) -> None:
    if not name or price is None:
        return
    name = " ".join(str(name).split()).strip()
    if not name:
        return
    if not _price_ok(price):
        logger.debug(
            "[%s] Prix ignoré (hors plage %s–%s\u00a0€) : %s\u00a0€ — %s",
            source,
            MIN_PRICE_EUR,
            MAX_PRICE_EUR,
            price,
            name[:60],
        )
        return
    out.append({"name": name, "price": round(price, 2), "source": source})


def _fetch_html(url: str, timeout: int = 15) -> str | None:
    try:
        r = SESSION.get(url, timeout=timeout)
        r.raise_for_status()
        return r.text
    except requests.RequestException as e:
        logger.error("Échec requête HTTP %s : %s", url, e)
        return None


def _extract_prices_from_text(text: str) -> list[float]:
    found: list[float] = []
    for m in _PRICE_EUR_RE.finditer(text):
        p = _parse_price_eur(m.group(1))
        if p is not None:
            found.append(p)
    return found


def _dedupe_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, float]] = set()
    out: list[dict[str, Any]] = []
    for it in items:
        key = (it["name"], it["price"])
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def scrape_vinted(
    url: str = "https://www.vinted.fr/catalog?search_text=sneakers&catalog[]=76",
    timeout: int = 15,
) -> list[dict[str, Any]]:
    """
    Récupère des annonces Vinted (nom + prix).

    Retourne une liste de dicts : ``{"name", "price", "source": "vinted"}``.
    """
    source = "vinted"
    out: list[dict[str, Any]] = []
    try:
        html = _fetch_html(url, timeout=timeout)
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")

        # 1) Données embarquées (Next / pré-rendu) si présentes
        for script in soup.find_all("script", type="application/json"):
            if not script.string:
                continue
            try:
                data = json.loads(script.string)
            except json.JSONDecodeError:
                continue
            _walk_json_for_items(data, out, source)

        # 2) Balises article / liens catalogue courants
        for article in soup.find_all(["article", "div"], attrs={"data-testid": True}):
            title_el = article.find(attrs={"data-testid": re.compile(r"title|name", re.I)})
            price_el = article.find(
                string=_PRICE_EUR_RE
            ) or article.find(class_=re.compile(r"price|amount", re.I))
            name = None
            if title_el:
                name = title_el.get_text(strip=True) if hasattr(title_el, "get_text") else str(title_el).strip()
            if not name:
                a = article.find("a", href=True)
                if a:
                    name = a.get_text(strip=True)
            price = None
            if price_el and hasattr(price_el, "get_text"):
                price = _parse_price_eur(price_el.get_text(" ", strip=True))
            if price is None:
                txt = article.get_text(" ", strip=True)
                prices = _extract_prices_from_text(txt)
                if prices:
                    price = min(prices)
            _append_if_ok(out, name, price, source)

        # 3) Fallback : extraire paires titre/prix depuis blocs contenant €
        if not out:
            for card in soup.select("a[href*='/items/']"):
                name = card.get("title") or card.get_text(" ", strip=True)
                prices = _extract_prices_from_text(card.get_text(" ", strip=True))
                if name and prices:
                    _append_if_ok(out, name, min(prices), source)

        result = _dedupe_items(out)
        logger.info(
            "scrape_vinted : %d annonce(s) retenue(s) (plage %s–%s\u00a0€)",
            len(result),
            MIN_PRICE_EUR,
            MAX_PRICE_EUR,
        )
        return result

    except Exception as e:
        logger.exception("scrape_vinted : erreur inattendue : %s", e)
        return []


def _walk_json_for_items(obj: Any, out: list[dict[str, Any]], source: str, depth: int = 0) -> None:
    if depth > 18:
        return
    if isinstance(obj, dict):
        title = obj.get("title") or obj.get("name") or obj.get("product_title")
        price_data = (
            obj.get("price")
            or obj.get("total_item_price")
            or obj.get("amount")
        )
        price: float | None = None
        if isinstance(price_data, dict):
            price = _parse_price_eur(price_data.get("amount") or price_data.get("value"))
        elif price_data is not None:
            price = _parse_price_eur(price_data)
        if isinstance(title, str) and price is not None:
            _append_if_ok(out, title, price, source)
        for v in obj.values():
            _walk_json_for_items(v, out, source, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _walk_json_for_items(v, out, source, depth + 1)


def scrape_leboncoin(
    url: str = "https://www.leboncoin.fr/recherche?text=sneakers&category=9",
    timeout: int = 15,
) -> list[dict[str, Any]]:
    """
    Récupère des annonces Leboncoin (nom + prix).

    Retourne une liste de dicts : ``{"name", "price", "source": "leboncoin"}``.
    """
    source = "leboncoin"
    out: list[dict[str, Any]] = []
    try:
        html = _fetch_html(url, timeout=timeout)
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")

        for script in soup.find_all("script", id="__NEXT_DATA__"):
            if not script.string:
                continue
            try:
                data = json.loads(script.string)
                _walk_json_for_items(data, out, source)
            except json.JSONDecodeError:
                logger.debug("scrape_leboncoin : JSON __NEXT_DATA__ illisible")

        for article in soup.find_all("article"):
            h2 = article.find(["h2", "h3", "p"])
            name = h2.get_text(strip=True) if h2 else None
            txt = article.get_text(" ", strip=True)
            prices = _extract_prices_from_text(txt)
            price = min(prices) if prices else None
            _append_if_ok(out, name, price, source)

        if not out:
            for a in soup.select('a[data-test-id="classified-link"], a[href*="/ad/"]'):
                name = a.get_text(" ", strip=True)
                prices = _extract_prices_from_text(a.get_text(" ", strip=True))
                if name and prices:
                    _append_if_ok(out, name, min(prices), source)

        result = _dedupe_items(out)
        logger.info(
            "scrape_leboncoin : %d annonce(s) retenue(s) (plage %s–%s\u00a0€)",
            len(result),
            MIN_PRICE_EUR,
            MAX_PRICE_EUR,
        )
        return result

    except Exception as e:
        logger.exception("scrape_leboncoin : erreur inattendue : %s", e)
        return []


def scrape_2ememain(
    url: str = "https://www.2ememain.be/l/schoenen-sportschoenen-sneakers/",
    timeout: int = 15,
) -> list[dict[str, Any]]:
    """
    Récupère des annonces 2ememain (nom + prix).

    Retourne une liste de dicts : ``{"name", "price", "source": "2ememain"}``.
    """
    source = "2ememain"
    out: list[dict[str, Any]] = []
    try:
        html = _fetch_html(url, timeout=timeout)
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")

        for script in soup.find_all("script", type="application/ld+json"):
            if not script.string:
                continue
            try:
                data = json.loads(script.string)
                if isinstance(data, dict) and data.get("@type") == "Product":
                    name = data.get("name")
                    off = data.get("offers")
                    price = None
                    if isinstance(off, dict):
                        price = _parse_price_eur(off.get("price"))
                    _append_if_ok(out, str(name) if name else None, price, source)
                elif isinstance(data, list):
                    for block in data:
                        if isinstance(block, dict) and block.get("@type") == "Product":
                            name = block.get("name")
                            off = block.get("offers")
                            price = None
                            if isinstance(off, dict):
                                price = _parse_price_eur(off.get("price"))
                            _append_if_ok(out, str(name) if name else None, price, source)
            except json.JSONDecodeError:
                continue

        for article in soup.find_all(["article", "li"]):
            a = article.find("a", href=True)
            name = a.get_text(" ", strip=True) if a else None
            txt = article.get_text(" ", strip=True)
            prices = _extract_prices_from_text(txt)
            if name and prices:
                _append_if_ok(out, name, min(prices), source)

        result = _dedupe_items(out)
        logger.info(
            "scrape_2ememain : %d annonce(s) retenue(s) (plage %s–%s\u00a0€)",
            len(result),
            MIN_PRICE_EUR,
            MAX_PRICE_EUR,
        )
        return result

    except Exception as e:
        logger.exception("scrape_2ememain : erreur inattendue : %s", e)
        return []


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    print("Vinted:", len(scrape_vinted()))
    print("Leboncoin:", len(scrape_leboncoin()))
    print("2ememain:", len(scrape_2ememain()))
