"""
Envoi WhatsApp multi-backend.

Ordre de priorité :
  1. CallMeBot   (CALLMEBOT_API_KEY dans .env) — gratuit, API HTTP simple
  2. Twilio      (TWILIO_ACCOUNT_SID + TWILIO_AUTH_TOKEN + TWILIO_WHATSAPP_FROM) — fiable, payant
  3. WhatsApp Web via Playwright (session persistée) — dernier recours, fragile

Pour activer CallMeBot :
  1. Envoie "I allow callmebot to send me messages" au +34 644 45 03 24 sur WhatsApp
  2. Tu recevras une clé API (ex: 1234567)
  3. Ajoute CALLMEBOT_API_KEY=1234567 dans .env et relance le service

Pour activer Twilio :
  Décommente TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_WHATSAPP_FROM dans .env
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
PENDING_NOTIF_PATH = _ROOT / "data" / "pending_notifications.json"
WHATSAPP_SESSION_DIR = _ROOT / "data" / "whatsapp_session"

_SEND_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Queue helpers
# ---------------------------------------------------------------------------

def _load_pending() -> list[dict]:
    if not PENDING_NOTIF_PATH.is_file():
        return []
    try:
        raw = json.loads(PENDING_NOTIF_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            return [x for x in raw if isinstance(x, dict)]
    except Exception as e:  # noqa: BLE001
        logger.warning("whatsapp_sender: lecture queue impossible: %s", e)
    return []


def _save_pending(items: list[dict]) -> None:
    try:
        PENDING_NOTIF_PATH.parent.mkdir(parents=True, exist_ok=True)
        PENDING_NOTIF_PATH.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        logger.warning("whatsapp_sender: écriture queue impossible: %s", e)


# ---------------------------------------------------------------------------
# Backend 1 — CallMeBot (gratuit, recommandé)
# ---------------------------------------------------------------------------

def _send_via_callmebot(digits: str, message: str) -> bool:
    """
    Envoie via CallMeBot API.
    Nécessite CALLMEBOT_API_KEY dans .env.
    Activation : envoyer "I allow callmebot to send me messages" au +34 644 45 03 24.
    """
    api_key = os.getenv("CALLMEBOT_API_KEY", "").strip()
    if not api_key:
        return False
    try:
        url = "https://api.callmebot.com/whatsapp.php"
        params = {
            "phone": f"+{digits}",
            "text": message,
            "apikey": api_key,
        }
        r = requests.get(url, params=params, timeout=20)
        if r.status_code == 200 and ("Message queued" in r.text or "Message sent" in r.text or "OK" in r.text.upper()):
            logger.info("whatsapp_sender [CallMeBot]: message envoyé à +%s", digits)
            return True
        logger.warning("whatsapp_sender [CallMeBot]: réponse inattendue (status=%s): %s", r.status_code, r.text[:200])
        return False
    except Exception as e:  # noqa: BLE001
        logger.warning("whatsapp_sender [CallMeBot]: erreur: %s", e)
        return False


# ---------------------------------------------------------------------------
# Backend 2 — Twilio WhatsApp
# ---------------------------------------------------------------------------

def _send_via_twilio(digits: str, message: str) -> bool:
    """
    Envoie via Twilio WhatsApp API.
    Nécessite TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM dans .env.
    Sandbox : le destinataire doit avoir rejoint le sandbox Twilio.
    """
    account_sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    from_number = os.getenv("TWILIO_WHATSAPP_FROM", "").strip()
    if not account_sid or not auth_token or not from_number:
        return False
    try:
        url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
        data = {
            "From": from_number,
            "To": f"whatsapp:+{digits}",
            "Body": message,
        }
        r = requests.post(url, data=data, auth=(account_sid, auth_token), timeout=20)
        if r.status_code in (200, 201):
            sid = r.json().get("sid", "")
            logger.info("whatsapp_sender [Twilio]: message envoyé sid=%s à +%s", sid, digits)
            return True
        logger.warning("whatsapp_sender [Twilio]: erreur %s: %s", r.status_code, r.text[:300])
        return False
    except Exception as e:  # noqa: BLE001
        logger.warning("whatsapp_sender [Twilio]: erreur: %s", e)
        return False


# ---------------------------------------------------------------------------
# Backend 3 — WhatsApp Web Playwright (session persistée)
# ---------------------------------------------------------------------------

_PLAYWRIGHT_CHROME_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--mute-audio",
    "--disable-background-networking",
]


def _send_via_playwright(digits: str, message: str) -> bool:
    """
    Envoie via WhatsApp Web (session Playwright persistée).
    Fragile : nécessite une session QR active et peut échouer silencieusement.
    """
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except Exception as e:  # noqa: BLE001
        logger.warning("whatsapp_sender [Playwright]: indisponible: %s", e)
        return False

    wa_url = f"https://web.whatsapp.com/send?phone={digits}&text={quote(message, safe='')}"
    with _SEND_LOCK:
        try:
            WHATSAPP_SESSION_DIR.mkdir(parents=True, exist_ok=True)
            with sync_playwright() as p:
                context = p.chromium.launch_persistent_context(
                    user_data_dir=str(WHATSAPP_SESSION_DIR),
                    headless=True,
                    args=_PLAYWRIGHT_CHROME_ARGS,
                )
                page = context.new_page()
                page.goto(wa_url, wait_until="domcontentloaded", timeout=45000)

                # Session non connectée → QR visible
                if page.locator('canvas[aria-label*="Scan me"], [data-testid="qrcode"]').count() > 0:
                    logger.warning("whatsapp_sender [Playwright]: session non connectée (QR requis).")
                    context.close()
                    return False

                # Attendre que la boîte de texte soit prête
                try:
                    page.wait_for_selector(
                        '[data-testid="conversation-compose-box-input"], [contenteditable="true"][data-tab="10"]',
                        timeout=15000,
                    )
                except PlaywrightTimeoutError:
                    logger.warning("whatsapp_sender [Playwright]: boîte de texte introuvable pour +%s", digits)
                    context.close()
                    return False

                # Cliquer sur le bouton envoyer
                try:
                    send_btn = page.locator('[data-icon="send"], [data-testid="send"], [aria-label="Envoyer"], [aria-label="Send"]').first
                    send_btn.click(timeout=10000)
                except PlaywrightTimeoutError:
                    # Fallback : touche Entrée
                    page.keyboard.press("Enter")

                # Vérifier que le message a bien été envoyé (icône de livraison)
                sent_ok = False
                try:
                    page.wait_for_selector(
                        '[data-icon="msg-check"], [data-icon="msg-dblcheck"], [data-testid="msg-time"]',
                        timeout=8000,
                    )
                    sent_ok = True
                except PlaywrightTimeoutError:
                    # On considère envoyé si pas d'erreur visible
                    error_visible = page.locator('[data-icon="alert-phone"], .invalid-number').count() > 0
                    sent_ok = not error_visible

                context.close()
                if sent_ok:
                    logger.info("whatsapp_sender [Playwright]: message envoyé à +%s", digits)
                else:
                    logger.warning("whatsapp_sender [Playwright]: envoi incertain pour +%s", digits)
                return sent_ok

        except Exception as e:  # noqa: BLE001
            logger.warning("whatsapp_sender [Playwright]: envoi échoué to=+%s err=%s", digits, e)
            return False


# ---------------------------------------------------------------------------
# Interface publique
# ---------------------------------------------------------------------------

def send_whatsapp_message(to: str, message: str) -> bool:
    """
    Envoie un message WhatsApp.
    Essaie CallMeBot → Twilio → Playwright dans cet ordre.
    Retourne True uniquement si un backend confirme l'envoi.
    """
    digits = "".join(c for c in str(to or "") if c.isdigit())
    message = str(message or "").strip()
    if not digits or not message:
        return False

    # Backend 1 : CallMeBot
    if os.getenv("CALLMEBOT_API_KEY", "").strip():
        if _send_via_callmebot(digits, message):
            return True
        logger.warning("whatsapp_sender: CallMeBot échoué, essai Twilio...")

    # Backend 2 : Twilio
    if os.getenv("TWILIO_ACCOUNT_SID", "").strip() and os.getenv("TWILIO_AUTH_TOKEN", "").strip():
        if _send_via_twilio(digits, message):
            return True
        logger.warning("whatsapp_sender: Twilio échoué, essai Playwright...")

    # Backend 3 : WhatsApp Web Playwright (dernier recours)
    return _send_via_playwright(digits, message)


def get_active_backend() -> str:
    """Retourne le nom du backend actif (pour diagnostic)."""
    if os.getenv("CALLMEBOT_API_KEY", "").strip():
        return "callmebot"
    if os.getenv("TWILIO_ACCOUNT_SID", "").strip() and os.getenv("TWILIO_AUTH_TOKEN", "").strip():
        return "twilio"
    return "playwright"


def process_pending_notifications() -> int:
    """
    Traite la queue pending_notifications.json.
    Marque sent=True en succès. Retourne le nombre d'envois réussis.
    """
    items = _load_pending()
    if not items:
        return 0
    sent_count = 0
    changed = False
    for item in items:
        if item.get("sent") is True:
            continue
        to = item.get("to") or ""
        msg = item.get("message") or ""
        ok = send_whatsapp_message(str(to), str(msg))
        if ok:
            item["sent"] = True
            item["sent_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            item["backend"] = get_active_backend()
            sent_count += 1
            changed = True
    if changed:
        _save_pending(items)
    return sent_count
