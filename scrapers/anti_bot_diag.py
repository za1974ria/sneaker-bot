from __future__ import annotations

import os
import re
import random
from datetime import datetime
from pathlib import Path

import requests


_BLOCK_PATTERNS = (
    r"captcha",
    r"cloudflare",
    r"access denied",
    r"forbidden",
    r"bot detection",
    r"verify you are human",
    r"robot check",
    r"temporarily blocked",
)


def detect_block_reason(html: str) -> str:
    if not html:
        return "empty_html"
    low = html.lower()
    for pat in _BLOCK_PATTERNS:
        if re.search(pat, low):
            return pat
    return ""


def dump_snapshot(site: str, url: str, html: str, reason: str) -> str:
    """Writes an HTML snapshot only when SCRAPER_DIAG_KO=1."""
    if str(os.getenv("SCRAPER_DIAG_KO", "0")).strip() != "1":
        return ""
    root = Path(__file__).resolve().parents[1] / "logs" / "scraper_diag"
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe_site = "".join(ch if ch.isalnum() else "_" for ch in site).strip("_") or "site"
    path = root / f"{stamp}_{safe_site}.html"
    body = html or ""
    meta = f"<!-- site={site} url={url} reason={reason} -->\n"
    path.write_text(meta + body, encoding="utf-8")
    return str(path)


UA_POOL = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
)


def realistic_headers() -> dict[str, str]:
    ua = random.choice(UA_POOL)
    return {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-User": "?1",
        "Sec-Fetch-Dest": "document",
        "Connection": "keep-alive",
    }


def fetch_with_rotating_headers(url: str, timeout: int = 20) -> str:
    try:
        r = requests.get(url, headers=realistic_headers(), timeout=timeout, allow_redirects=True)
        if r.status_code >= 400:
            return ""
        return r.text or ""
    except Exception:
        return ""


def fetch_playwright_stealth(url: str, *, source_name: str) -> str:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return ""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            try:
                _proxy_url = os.getenv("PROXY_URL", "").strip()
                _ctx_kwargs: dict = {
                    "user_agent": random.choice(UA_POOL),
                    "locale": "fr-FR",
                    "ignore_https_errors": True,
                    "viewport": {"width": 1366, "height": 900},
                    "extra_http_headers": {
                        "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
                    },
                }
                if _proxy_url:
                    _ctx_kwargs["proxy"] = {"server": _proxy_url}
                context = browser.new_context(**_ctx_kwargs)
                page = context.new_page()
                try:
                    page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
                    try:
                        page.goto(url, wait_until="networkidle", timeout=45000)
                    except Exception:
                        page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(1500)
                    page.mouse.wheel(0, 1600)
                    page.wait_for_timeout(900)
                    return page.content() or ""
                finally:
                    context.close()
            finally:
                browser.close()
    except Exception:
        return ""


def fetch_via_scraperapi(url: str, timeout: int = 35) -> str:
    key = str(os.getenv("SCRAPERAPI_KEY", "")).strip()
    if not key:
        return ""
    endpoint = "http://api.scraperapi.com/"
    try:
        r = requests.get(
            endpoint,
            params={"api_key": key, "url": url, "country_code": "fr", "keep_headers": "true"},
            timeout=timeout,
        )
        if r.status_code >= 400:
            return ""
        return r.text or ""
    except Exception:
        return ""
