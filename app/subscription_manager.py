"""
Gestionnaire d'abonnements SneakerBot.
Flux : Client soumet → Notification admin → Validation → Accès créés.
"""

from __future__ import annotations

import bcrypt
import json
import logging
import os
import secrets
import smtplib
import string
import uuid
from datetime import datetime, timezone
from html import escape
from urllib.parse import quote
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
SUBS_PATH = _ROOT / "data" / "subscriptions.json"
ACCESS_PATH = _ROOT / "data" / "access_control.json"
PENDING_NOTIF_PATH = _ROOT / "data" / "pending_notifications.json"

PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or "https://sneakerbot.shop").rstrip("/")


def public_base_url() -> str:
    """URL publique canonique (fallback stable si env absente)."""
    return (os.getenv("PUBLIC_BASE_URL") or PUBLIC_BASE_URL or "https://sneakerbot.shop").rstrip("/")


def get_admin_whatsapp_digits() -> str:
    """
    Numéro admin canonique pour liens wa.me.
    Priorité: ADMIN_WHATSAPP_NUMBER, puis ADMIN_WHATSAPP (format whatsapp:+...).
    """
    raw = (os.getenv("ADMIN_WHATSAPP_NUMBER") or "").strip()
    if not raw:
        raw = (os.getenv("ADMIN_WHATSAPP") or "").strip()
    digits = "".join(c for c in raw if c.isdigit())
    return digits or "213540388413"

PLANS: dict[str, dict[str, object]] = {
    "essai": {"price": 0, "euros": 0, "label": "Essai Gratuit"},
    "mensuel": {"price": 29, "euros": 29, "label": "Mensuel 29\u00a0€"},
    "annuel": {"price": 190, "euros": 190, "label": "Annuel 190\u00a0€"},
}


def _default_subs() -> dict[str, object]:
    return {"pending": [], "validated": [], "rejected": []}


def _load_subs() -> dict[str, object]:
    if not SUBS_PATH.is_file():
        return _default_subs()
    try:
        raw = json.loads(SUBS_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return _default_subs()
        out = _default_subs()
        for k in ("pending", "validated", "rejected"):
            v = raw.get(k)
            if isinstance(v, list):
                out[k] = v
        return out
    except Exception:
        logger.exception("Lecture subscriptions impossible")
        return _default_subs()


def _save_subs(data: dict[str, object]) -> None:
    try:
        SUBS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SUBS_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        logger.exception("Écriture subscriptions impossible")


def _collect_reserved_usernames() -> set[str]:
    """Lit ACCESS_PATH et renvoie l'ensemble des usernames présents dans users."""
    out: set[str] = set()
    if not ACCESS_PATH.is_file():
        return out
    try:
        raw = json.loads(ACCESS_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return out
        users = raw.get("users")
        if not isinstance(users, list):
            return out
        for u in users:
            if isinstance(u, dict):
                un = u.get("username")
                if isinstance(un, str) and un.strip():
                    out.add(un.strip())
    except Exception:
        logger.exception("Lecture usernames réservés impossible")
    return out


def _generate_credentials(name: str, reserved: set[str] | None = None) -> tuple[str, str]:
    """Génère un couple username / mot de passe (mot de passe 14 caractères)."""
    parts = (name or "").strip().split()
    first_name = (parts[0] if parts else "user").lower()
    first_name = "".join(c for c in first_name if c.isalnum())[:10] or "user"

    alphabet = string.ascii_letters + string.digits + "!@#$%&*"
    password = "".join(secrets.choice(alphabet) for _ in range(14))

    if reserved:
        username: str | None = None
        for _ in range(120):
            suffix = secrets.randbelow(9000) + 1000
            candidate = f"{first_name}{suffix}"
            if candidate not in reserved:
                username = candidate
                break
        if username is None:
            username = f"{first_name}{secrets.token_hex(3)}"
            while username in reserved:
                username = f"{first_name}{secrets.token_hex(3)}"
    else:
        suffix = secrets.randbelow(9000) + 1000
        username = f"{first_name}{suffix}"

    return username, password


def _generate_unique_credentials(name: str, taken_usernames: set[str]) -> tuple[str, str]:
    """Toujours un username absent de access_control ; mot de passe neuf à chaque appel."""
    for _ in range(128):
        u, p = _generate_credentials(name)
        if u not in taken_usernames:
            return u, p
    while True:
        u = f"user{secrets.token_hex(4)}"
        if u not in taken_usernames:
            alphabet = string.ascii_letters + string.digits + "!@#$%&*"
            p = "".join(secrets.choice(alphabet) for _ in range(14))
            return u, p


def submit_subscription(
    name: str,
    email: str = "",
    plan: str = "mensuel",
    reference: str = "",
    whatsapp: str = "",
    canal: str = "email",
) -> dict[str, object]:
    """Enregistre une demande d'abonnement et notifie l'admin (non bloquant).

    Plusieurs demandes avec le même email ou le même contact sont autorisées
    (nouvel abonnement, changement de plan, etc.).
    Les emails en double sont autorisés (plusieurs abonnements).
    """
    plan_key = (plan or "mensuel").strip().lower()
    if plan_key not in PLANS:
        plan_key = "mensuel"
    ref = (reference or "").strip()
    if plan_key != "essai" and not ref:
        raise ValueError("Référence de virement requise pour les offres payantes")

    canal_key = (canal or "email").strip().lower()
    if canal_key not in ("email", "whatsapp", "both"):
        canal_key = "email"
    email = (email or "").strip()
    wa_input = (whatsapp or "").strip()
    if canal_key in ("email", "both") and not email:
        raise ValueError("Adresse email requise pour ce mode de réception")
    if canal_key in ("whatsapp", "both") and not wa_input:
        raise ValueError("Numéro WhatsApp requis pour ce mode de réception")
    if not (name or "").strip():
        raise ValueError("Prénom / nom requis")

    sub_id = str(uuid.uuid4())[:8].upper()
    now = datetime.now(timezone.utc).isoformat()
    plan_info = PLANS.get(plan_key, PLANS["mensuel"])
    if not isinstance(plan_info, dict):
        plan_info = PLANS["mensuel"]

    wa = wa_input
    if wa and not wa.startswith("whatsapp:"):
        digits = "".join(c for c in wa if c.isdigit() or c == "+")
        if digits.startswith("+"):
            wa = f"whatsapp:{digits}"
        elif digits:
            wa = f"whatsapp:+{digits.lstrip('+')}"

    sub: dict[str, object] = {
        "id": sub_id,
        "name": (name or "").strip(),
        "email": email,
        "plan": plan_key,
        "amount": int(plan_info.get("price") or 0),
        "reference": ref if plan_key != "essai" else "",
        "whatsapp": wa,
        "canal": canal_key,
        "submitted_at": now,
        "status": "pending",
        "username": None,
        "password": None,
    }

    data = _load_subs()
    pending = list(data.get("pending") or [])
    pending.append(sub)
    data["pending"] = pending
    _save_subs(data)

    try:
        _notify_whatsapp_admin(sub)
    except Exception as e:
        logger.warning("WhatsApp notification: %s", e)

    try:
        _notify_email_admin(sub)
    except Exception as e:
        logger.warning("Email admin notification: %s", e)

    logger.info("Abonnement soumis : %s — %s — %s", sub_id, sub["name"], plan_key)
    return sub


def validate_subscription(sub_id: str) -> dict[str, object]:
    """Valide un abonnement, crée les accès et notifie le client."""
    data = _load_subs()
    pending = [s for s in (data.get("pending") or []) if isinstance(s, dict)]
    sub = next((s for s in pending if str(s.get("id")) == str(sub_id)), None)
    if not sub:
        raise ValueError(f"Abonnement {sub_id} introuvable")

    plan_key = str(sub.get("plan") or "mensuel")
    plan_info = PLANS.get(plan_key, PLANS["mensuel"])
    paid_euros = int(plan_info.get("euros") or 0) if isinstance(plan_info, dict) else 0
    paid_euros = max(0, paid_euros)

    if not ACCESS_PATH.is_file():
        raise ValueError("access_control.json introuvable")

    access = json.loads(ACCESS_PATH.read_text(encoding="utf-8"))
    if not isinstance(access, dict):
        access = {}
    users = access.get("users")
    if not isinstance(users, list):
        users = []
    reserved = _collect_reserved_usernames()
    username, password = _generate_credentials(str(sub.get("name") or ""), reserved)
    now_iso = datetime.now(timezone.utc).isoformat()
    hashed_pw = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    users.append(
        {
            "username": username,
            "password": hashed_pw,
            "role": "client",
            "active": True,
            "paid_comparison_euros": paid_euros,
            "plan": plan_key,
            "email": str(sub.get("email") or ""),
            "name": str(sub.get("name") or ""),
            "created_at": now_iso,
            "sub_id": str(sub.get("id") or ""),
        }
    )
    access["users"] = users
    ACCESS_PATH.write_text(json.dumps(access, ensure_ascii=False, indent=2), encoding="utf-8")

    sub = dict(sub)
    sub["status"] = "validated"
    sub["username"] = username
    sub["password"] = password
    sub["validated_at"] = now_iso

    data["pending"] = [s for s in pending if str(s.get("id")) != str(sub_id)]
    validated = list(data.get("validated") or [])
    validated.append(sub)
    data["validated"] = validated
    _save_subs(data)

    canal = str(sub.get("canal") or "email").strip().lower()
    if canal not in ("email", "whatsapp", "both"):
        canal = "email"

    if canal in ("email", "both") and sub.get("email"):
        logger.info("Envoi email client: %s", sub["email"])
        if not (sub.get("username") and sub.get("password")):
            logger.error(
                "❌ Email client skipped: username/password manquants avant envoi (sub_id=%s)",
                sub.get("id"),
            )
        else:
            try:
                _send_credentials_to_client(sub)
                logger.info("✅ Email client envoyé: %s", sub["email"])
            except Exception as e:  # noqa: BLE001
                logger.error("❌ Email client FAILED: %s", e)

    if canal in ("whatsapp", "both") and sub.get("whatsapp"):
        try:
            _send_whatsapp_client_wame(sub)
            logger.info("📱 WhatsApp accès prêt pour %s", sub["whatsapp"])
        except Exception as e:  # noqa: BLE001
            logger.error("WhatsApp client: %s", e)

    logger.info("Abonnement validé : %s — login=%s", sub_id, username)
    return sub


def reject_subscription(sub_id: str) -> dict[str, object]:
    """Refuse une demande : sort de pending, entrée dans rejected."""
    data = _load_subs()
    pending = [s for s in (data.get("pending") or []) if isinstance(s, dict)]
    sub = next((s for s in pending if str(s.get("id")) == str(sub_id)), None)
    if not sub:
        raise ValueError(f"Abonnement {sub_id} introuvable")

    sub = dict(sub)
    sub["status"] = "rejected"
    sub["rejected_at"] = datetime.now(timezone.utc).isoformat()

    data["pending"] = [s for s in pending if str(s.get("id")) != str(sub_id)]
    rejected = list(data.get("rejected") or [])
    rejected.append(sub)
    data["rejected"] = rejected
    _save_subs(data)
    logger.info("Abonnement refusé : %s", sub_id)
    return sub


def subscription_counts() -> dict[str, int]:
    data = _load_subs()
    return {
        "pending": len(data.get("pending") or []),
        "validated": len(data.get("validated") or []),
        "rejected": len(data.get("rejected") or []),
    }


def get_pending_subscriptions() -> list[dict[str, object]]:
    data = _load_subs()
    return [s for s in (data.get("pending") or []) if isinstance(s, dict)]


def get_all_subscriptions() -> dict[str, object]:
    return _load_subs()


def _whatsapp_admin_notify_body(sub: dict[str, object]) -> str:
    return (
        f"🔔 NOUVEAU CLIENT SNEAKERBOT\n\n"
        f"ID: {sub.get('id', '')}\n"
        f"Nom: {sub.get('name', '')}\n"
        f"Email: {sub.get('email', '')}\n"
        f"Plan: {sub.get('plan', '')} — {sub.get('amount', '')}\u00a0€\n"
        f"Référence: {sub.get('reference', '')}\n\n"
        f"✅ Valider sur:\n"
        f"{public_base_url()}/admin/subscriptions"
    )


def build_whatsapp_admin_wa_me_url(sub: dict[str, object]) -> str:
    digits = get_admin_whatsapp_digits()
    body = _whatsapp_admin_notify_body(sub)
    return f"https://wa.me/{digits}?text={quote(body, safe='')}"


def get_unsent_admin_notifications() -> list[dict[str, object]]:
    if not PENDING_NOTIF_PATH.is_file():
        return []
    try:
        raw = json.loads(PENDING_NOTIF_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return []
        return [x for x in raw if isinstance(x, dict) and x.get("sent") is not True]
    except Exception:
        logger.exception("Lecture pending_notifications impossible")
        return []


def _notify_whatsapp_admin(sub: dict[str, object]) -> None:
    """
    Ouvre un lien WhatsApp direct vers le numéro admin.
    Pas de Twilio — lien direct wa.me
    """
    admin_number = get_admin_whatsapp_digits()

    message = _whatsapp_admin_notify_body(sub)

    logger.info("📱 WhatsApp admin requis:\n%s", message)

    notif_path = PENDING_NOTIF_PATH
    notif_path.parent.mkdir(parents=True, exist_ok=True)
    notifs: list[object] = []
    if notif_path.is_file():
        try:
            raw = json.loads(notif_path.read_text(encoding="utf-8"))
            notifs = raw if isinstance(raw, list) else []
        except Exception:
            notifs = []

    notifs.append(
        {
            "type": "whatsapp_admin",
            "to": admin_number,
            "message": message,
            "sub_id": sub["id"],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "sent": False,
        }
    )
    notif_path.write_text(
        json.dumps(notifs, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _send_whatsapp_client_wame(sub: dict[str, object]) -> str | None:
    """
    Prépare un lien wa.me pour envoyer les accès
    au client via WhatsApp — sans Twilio.
    """
    client_number = sub.get("whatsapp", "")
    if not client_number:
        logger.info("Pas de numéro WhatsApp client")
        return None

    raw = str(client_number)
    number = (
        raw.replace("whatsapp:", "")
        .replace("+", "")
        .replace(" ", "")
        .strip()
    )
    number = "".join(c for c in number if c.isdigit())
    if not number:
        logger.info("Numéro WhatsApp client invalide après nettoyage")
        return None

    base_url = public_base_url()
    message = (
        f"✅ Vos accès SneakerBot sont prêts !\n\n"
        f"🔗 URL : {base_url}\n"
        f"👤 Login : {sub['username']}\n"
        f"🔑 Mot de passe : {sub['password']}\n\n"
        f"Connectez-vous ici :\n"
        f"{base_url}/login\n\n"
        f"Bonne utilisation ! 👟"
    )

    wa_url = f"https://wa.me/{number}?text={quote(message, safe='')}"

    notif_path = PENDING_NOTIF_PATH
    notif_path.parent.mkdir(parents=True, exist_ok=True)
    notifs: list[object] = []
    if notif_path.is_file():
        try:
            raw_json = json.loads(notif_path.read_text(encoding="utf-8"))
            notifs = raw_json if isinstance(raw_json, list) else []
        except Exception:
            notifs = []

    notifs.append(
        {
            "type": "whatsapp_client_credentials",
            "to": number,
            "wa_url": wa_url,
            "message": message,
            "sub_id": sub["id"],
            "client_name": sub["name"],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "sent": False,
        }
    )
    notif_path.write_text(
        json.dumps(notifs, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("📱 WhatsApp client prêt pour %s", number)
    return wa_url


def build_whatsapp_client_login_wa_me_url(
    *,
    raw_whatsapp: str,
    username: str,
    password: str,
) -> str | None:
    """Construit un lien wa.me client cohérent avec l'URL publique courante."""
    cleaned = raw_whatsapp.replace("whatsapp:", "").replace("+", "").replace(" ", "")
    number = "".join(c for c in cleaned if c.isdigit())
    if not number:
        return None
    base_url = public_base_url()
    message = (
        f"✅ Vos accès SneakerBot\n\n"
        f"🔗 {base_url}\n"
        f"👤 Login : {username}\n"
        f"🔑 Pass : {password}\n\n"
        f"{base_url}/login 👟"
    )
    return f"https://wa.me/{number}?text={quote(message, safe='')}"


def _notify_email_admin(sub: dict[str, object]) -> None:
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_pass = os.getenv("SMTP_PASS", "")
    admin_email = os.getenv("ADMIN_EMAIL", smtp_user)

    if not smtp_user or not smtp_pass:
        logger.warning("SMTP non configuré")
        return

    msg = MIMEMultipart("alternative")
    sid = str(sub.get("id", ""))
    msg["Subject"] = f"🔔 Nouveau client SneakerBot — {sid}"
    msg["From"] = smtp_user
    msg["To"] = admin_email

    wa_url = build_whatsapp_admin_wa_me_url(sub)
    wa_href = escape(wa_url, quote=True)

    public_url = public_base_url()
    html = f"""
    <html><body style="font-family:Arial;background:#0d0d0d;color:#fff;padding:20px">
    <h2 style="color:#00ff88">🔔 Nouveau Client SneakerBot</h2>
    <table style="border-collapse:collapse;width:100%">
        <tr><td style="padding:8px;color:#aaa">ID</td>
            <td style="padding:8px"><b>{sid}</b></td></tr>
        <tr><td style="padding:8px;color:#aaa">Nom</td>
            <td style="padding:8px">{sub.get("name")}</td></tr>
        <tr><td style="padding:8px;color:#aaa">Canal accès</td>
            <td style="padding:8px">{sub.get("canal") or "email"}</td></tr>
        <tr><td style="padding:8px;color:#aaa">Email</td>
            <td style="padding:8px">{sub.get("email") or "—"}</td></tr>
        <tr><td style="padding:8px;color:#aaa">WhatsApp</td>
            <td style="padding:8px">{sub.get("whatsapp") or "—"}</td></tr>
        <tr><td style="padding:8px;color:#aaa">Plan</td>
            <td style="padding:8px;color:#00ff88">
                <b>{sub.get("plan")} — {sub.get("amount")}\u00a0€</b></td></tr>
        <tr><td style="padding:8px;color:#aaa">Référence</td>
            <td style="padding:8px">{sub.get("reference")}</td></tr>
    </table>
    <br>
    <a href="{public_url}/admin/validate/{sid}"
       style="background:#00ff88;color:#000;padding:12px 24px;border-radius:8px;
              text-decoration:none;font-weight:bold;display:inline-block;margin-top:16px">
        ✅ Valider cet abonnement
    </a>
    <br>
    <a href="{wa_href}"
       style="background:#25D366;color:#fff;padding:10px 20px;border-radius:6px;
              text-decoration:none;display:inline-block;margin-top:10px">
        📱 Répondre sur WhatsApp
    </a>
    <p style="color:#666;margin-top:20px;font-size:12px">
        Vérifiez le virement avant de valider.
    </p>
    </body></html>
    """

    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, admin_email, msg.as_string())

    logger.info("Email admin envoyé pour %s", sid)


def _send_whatsapp_client_credentials(sub: dict[str, object]) -> None:
    """Notification WhatsApp au client (Twilio) si numéro enregistré sur la demande."""
    to_wa = str(sub.get("whatsapp") or "").strip()
    if not to_wa or not to_wa.startswith("whatsapp:"):
        return

    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    from_whatsapp = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")

    if not account_sid or not auth_token:
        logger.debug("Twilio non configuré — pas de WhatsApp client")
        return

    from twilio.rest import Client

    prenom = str((sub.get("name") or "").split()[0] or "Bonjour")
    user = str(sub.get("username") or "")
    body = (
        f"👟 SneakerBot — Bonjour {prenom},\n\n"
        f"Vos identifiants sont prêts.\n"
        f"Identifiant : {user}\n\n"
        f"Le mot de passe vous a été envoyé par email "
        f"({sub.get('email')}) pour des raisons de sécurité.\n\n"
        f"Connexion : {public_url}/login"
    )

    try:
        client = Client(account_sid, auth_token)
        client.messages.create(body=body, from_=from_whatsapp, to=to_wa)
        logger.info("WhatsApp credentials envoyé au client %s", to_wa[:24])
    except Exception as e:  # noqa: BLE001
        logger.warning("WhatsApp client credentials: %s", e)


def _send_credentials_to_client(sub: dict[str, object]) -> None:
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_pass = os.getenv("SMTP_PASS", "")

    if not smtp_user or not smtp_pass:
        logger.warning("SMTP non configuré — email client non envoyé")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "✅ Vos accès SneakerBot sont prêts !"
    msg["From"] = smtp_user
    msg["To"] = str(sub.get("email") or "")

    prenom = str((sub.get("name") or "").split()[0] or "Bonjour")
    user = str(sub.get("username") or "")
    pw = str(sub.get("password") or "")

    html = f"""
    <html><body style="font-family:Arial;background:#0d0d0d;color:#fff;padding:20px">
    <h2 style="color:#00ff88">👟 Bienvenue sur SneakerBot !</h2>
    <p>Bonjour {prenom},</p>
    <p>Votre abonnement <b style="color:#00ff88">{sub.get("plan")}</b>
       est activé. Voici vos accès :</p>
    <div style="background:#111;border:1px solid #00ff88;border-radius:8px;padding:16px;margin:20px 0">
        <p style="margin:4px 0">🔗 <b>URL :</b>
            <a href="{public_url}" style="color:#00ff88">{public_url}</a>
        </p>
        <p style="margin:4px 0">👤 <b>Identifiant :</b>
            <code style="color:#00ff88">{user}</code>
        </p>
        <p style="margin:4px 0">🔑 <b>Mot de passe :</b>
            <code style="color:#00ff88">{pw}</code>
        </p>
    </div>
    <a href="{public_url}/login"
       style="background:#00ff88;color:#000;padding:12px 24px;border-radius:8px;
              text-decoration:none;font-weight:bold;display:inline-block">
        🚀 Accéder à mon dashboard
    </a>
    <p style="color:#666;margin-top:20px;font-size:12px">
        Conservez ces informations précieusement.<br>
        Support : {public_url}
    </p>
    </body></html>
    """

    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, str(sub.get("email")), msg.as_string())

    logger.info("Accès envoyés à %s (login=%s)", sub.get("email"), user)
