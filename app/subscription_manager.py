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
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import quote
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from app.db import DatabaseManager

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = _ROOT / "data" / "sneakerbot.db"
_db = DatabaseManager(DB_PATH)
PENDING_NOTIF_PATH = _ROOT / "data" / "pending_notifications.json"
ADMIN_WA_ME_NUMBER = "213540388413"

PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or "https://sneakerbot.shop").rstrip("/")


def public_base_url() -> str:
    """URL publique canonique (fallback stable si env absente)."""
    return (os.getenv("PUBLIC_BASE_URL") or PUBLIC_BASE_URL or "https://sneakerbot.shop").rstrip("/")


def get_admin_whatsapp_digits() -> str:
    """Numéro WhatsApp admin unique (solution wa.me définitive)."""
    return ADMIN_WA_ME_NUMBER

PLANS: dict[str, dict[str, object]] = {
    "essai": {"price": 0, "euros": 0, "label": "Essai Gratuit"},
    "mensuel": {"price": 9, "euros": 9, "label": "Mensuel 9\u00a0€/mois (3 mois) puis 19\u00a0€/mois"},
    "annuel": {"price": 149, "euros": 149, "label": "Annuel 149\u00a0€"},
}


def _default_subs() -> dict[str, object]:
    return {"pending": [], "validated": [], "rejected": []}


def _load_subs() -> dict[str, object]:
    try:
        out = _default_subs()
        out["pending"] = _db.list_subscriptions_by_status("pending")
        out["validated"] = _db.list_subscriptions_by_status("validated")
        out["rejected"] = _db.list_subscriptions_by_status("rejected")
        return out
    except Exception:
        logger.exception("Lecture subscriptions impossible")
        return _default_subs()


def _save_subs(data: dict[str, object]) -> None:
    try:
        for status in ("pending", "validated", "rejected"):
            rows = data.get(status) or []
            if not isinstance(rows, list):
                continue
            for item in rows:
                if not isinstance(item, dict):
                    continue
                _db.upsert_subscription(item, status=status)
    except Exception:
        logger.exception("Écriture subscriptions impossible")


def _collect_reserved_usernames() -> set[str]:
    """Lit access_control (SQLite) et renvoie l'ensemble des usernames existants."""
    out: set[str] = set()
    try:
        raw = _db.get_access_control(default_users=[], default_sales_mode="open")
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

    email = (email or "").strip()
    if not email:
        raise ValueError("Adresse email requise")
    if not (name or "").strip():
        raise ValueError("Prénom / nom requis")

    sub_id = str(uuid.uuid4())[:8].upper()
    now = datetime.now(timezone.utc).isoformat()
    plan_info = PLANS.get(plan_key, PLANS["mensuel"])
    if not isinstance(plan_info, dict):
        plan_info = PLANS["mensuel"]

    sub: dict[str, object] = {
        "id": sub_id,
        "name": (name or "").strip(),
        "email": email,
        "plan": plan_key,
        "amount": int(plan_info.get("price") or 0),
        "reference": ref if plan_key != "essai" else "",
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

    access = _db.get_access_control(default_users=[], default_sales_mode="open")
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
    if not _db.save_access_control(access):
        raise ValueError("Écriture access_control impossible")

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

    if sub.get("email"):
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

    _send_whatsapp_client_wame(sub)
    logger.info("📱 Lien wa.me credentials préparé (admin)")

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


def delete_subscription(sub_id: str) -> dict[str, object]:
    """
    Supprime définitivement une demande d'abonnement de toutes les listes.
    N'affecte pas les accès (access_control) déjà créés lors d'une validation.
    Logue l'action sans exposer de token.
    """
    data = _load_subs()
    deleted: dict[str, object] | None = None
    for status in ("pending", "validated", "rejected"):
        items = [s for s in (data.get(status) or []) if isinstance(s, dict)]
        for s in items:
            if str(s.get("id")) == str(sub_id):
                deleted = dict(s)
                break
        if deleted is not None:
            data[status] = [s for s in items if str(s.get("id")) != str(sub_id)]
            break
    if deleted is None:
        raise ValueError(f"Abonnement {sub_id} introuvable")
    _save_subs(data)
    logger.info("Abonnement supprimé : %s (status=%s, name=%s)", sub_id, deleted.get("status"), deleted.get("name"))
    return deleted


def grant_free_trial(
    sub_id: str,
    *,
    days: int = 14,
    trial_start: str | None = None,
    trial_end: str | None = None,
) -> dict[str, object]:
    """
    Offre un essai gratuit à un abonnement en attente.
    Crée les accès (username/password) et marque le statut comme 'trial'.
    days : durée en jours (ignoré si trial_end explicite).
    trial_start / trial_end : dates ISO optionnelles (sinon calculées depuis maintenant).
    """
    data = _load_subs()
    pending = [s for s in (data.get("pending") or []) if isinstance(s, dict)]
    sub = next((s for s in pending if str(s.get("id")) == str(sub_id)), None)
    if not sub:
        # Chercher aussi dans validated (re-grant possible)
        validated = [s for s in (data.get("validated") or []) if isinstance(s, dict)]
        sub = next((s for s in validated if str(s.get("id")) == str(sub_id)), None)
        if not sub:
            raise ValueError(f"Abonnement {sub_id} introuvable dans pending/validated")

    now_iso = datetime.now(timezone.utc).isoformat()
    start_dt = datetime.now(timezone.utc)
    if trial_start:
        try:
            start_dt = datetime.fromisoformat(trial_start.replace("Z", "+00:00"))
        except Exception:
            pass

    if trial_end:
        end_iso = trial_end
    else:
        end_dt = start_dt + timedelta(days=max(1, int(days)))
        end_iso = end_dt.isoformat()

    # Générer les accès si pas encore présents
    access = _db.get_access_control(default_users=[], default_sales_mode="open")
    if not isinstance(access, dict):
        access = {}
    users = access.get("users")
    if not isinstance(users, list):
        users = []

    existing_username = sub.get("username")
    if not existing_username:
        reserved = _collect_reserved_usernames()
        username, password = _generate_credentials(str(sub.get("name") or ""), reserved)
        hashed_pw = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        users.append({
            "username": username,
            "password": hashed_pw,
            "role": "client",
            "active": True,
            "paid_comparison_euros": 0,
            "plan": "essai",
            "email": str(sub.get("email") or ""),
            "name": str(sub.get("name") or ""),
            "created_at": now_iso,
            "sub_id": str(sub.get("id") or ""),
            "trial_end": end_iso,
        })
        access["users"] = users
        if not _db.save_access_control(access):
            raise ValueError("Écriture access_control impossible")
    else:
        username = str(existing_username)
        password = None  # Ne pas re-générer si déjà validé

    sub = dict(sub)
    sub["status"] = "trial"
    sub["plan"] = "essai"
    sub["amount"] = 0
    sub["trial_start"] = start_dt.isoformat()
    sub["trial_end"] = end_iso
    sub["trial_days"] = max(1, int(days))
    if not sub.get("username"):
        sub["username"] = username
        sub["password"] = password
    sub["validated_at"] = now_iso

    # Retire de pending, ajoute à validated
    data["pending"] = [s for s in pending if str(s.get("id")) != str(sub_id)]
    validated = [s for s in (data.get("validated") or []) if isinstance(s, dict) and str(s.get("id")) != str(sub_id)]
    validated.append(sub)
    data["validated"] = validated
    _save_subs(data)

    if password and sub.get("email"):
        try:
            _send_credentials_to_client(sub)
        except Exception as e:
            logger.warning("Email essai gratuit: %s", e)

    logger.info("Essai gratuit accordé : %s — %s jours → exp %s", sub_id, days, end_iso)
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


def whatsapp_notify(
    *,
    message: str,
    notif_type: str,
    sub_id: str,
    to_number: str | None = None,
    extra: dict[str, object] | None = None,
) -> str | None:
    """
    Construit un lien wa.me, l'enregistre dans pending_notifications
    et retourne l'URL générée.
    """
    number = "".join(c for c in (to_number or get_admin_whatsapp_digits()) if c.isdigit())
    if not number:
        logger.warning("whatsapp_notify: numéro invalide")
        return None

    wa_url = f"https://wa.me/{number}?text={quote(message, safe='')}"
    notif: dict[str, object] = {
        "type": notif_type,
        "to": number,
        "wa_url": wa_url,
        "message": message,
        "sub_id": sub_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "sent": False,
    }
    if extra:
        notif.update(extra)

    notif_path = PENDING_NOTIF_PATH
    notif_path.parent.mkdir(parents=True, exist_ok=True)
    notifs: list[object] = []
    if notif_path.is_file():
        try:
            raw = json.loads(notif_path.read_text(encoding="utf-8"))
            notifs = raw if isinstance(raw, list) else []
        except Exception:
            notifs = []
    notifs.append(notif)
    notif_path.write_text(json.dumps(notifs, ensure_ascii=False, indent=2), encoding="utf-8")
    # Tentative d'envoi immédiate (non bloquante pour le flux métier).
    try:
        from app.whatsapp_sender import process_pending_notifications

        process_pending_notifications()
    except Exception as e:  # noqa: BLE001
        logger.debug("whatsapp_notify: sender auto skip: %s", e)
    return wa_url


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
    Lien direct wa.me uniquement.
    """
    message = _whatsapp_admin_notify_body(sub)
    wa_url = whatsapp_notify(
        message=message,
        notif_type="whatsapp_admin",
        sub_id=str(sub.get("id") or ""),
        to_number=get_admin_whatsapp_digits(),
    )
    if wa_url:
        logger.info("📱 WhatsApp admin requis: %s", wa_url)


def _send_whatsapp_client_wame(sub: dict[str, object]) -> str | None:
    """
    Prépare un lien wa.me pour envoyer les accès
    au client via WhatsApp (redirigé vers l'admin).
    """
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

    wa_url = whatsapp_notify(
        message=message,
        notif_type="whatsapp_client_credentials",
        sub_id=str(sub.get("id") or ""),
        to_number=get_admin_whatsapp_digits(),
        extra={
            "client_name": sub.get("name") or "",
            "client_email": sub.get("email") or "",
        },
    )
    logger.info("📱 WhatsApp credentials prêts pour admin")
    return wa_url


def build_whatsapp_client_login_wa_me_url(
    *,
    username: str,
    password: str,
) -> str | None:
    """Construit un lien wa.me credentials vers le numéro admin canonique."""
    base_url = public_base_url()
    message = (
        f"✅ Vos accès SneakerBot\n\n"
        f"🔗 {base_url}\n"
        f"👤 Login : {username}\n"
        f"🔑 Pass : {password}\n\n"
        f"{base_url}/login 👟"
    )
    number = get_admin_whatsapp_digits()
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

    public_url = public_base_url()
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


# ── CRM : Offrir accès direct + email premium ────────────────────────────────

def create_direct_access(
    *,
    name: str,
    email: str,
    days: int = 7,
    personal_message: str = "",
) -> dict[str, object]:
    """
    Crée un accès découverte directement depuis l'admin (sans demande préalable).
    Génère username + mot de passe, hache le mot de passe pour access_control,
    envoie l'email premium, retourne le sub dict (avec password en clair pour WA).
    """
    name = (name or "").strip()
    email = (email or "").strip()
    if not name:
        raise ValueError("Prénom / Nom requis")
    if not email:
        raise ValueError("Email requis")
    days = max(1, min(int(days), 3650))

    now_iso = datetime.now(timezone.utc).isoformat()
    trial_end = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    sub_id = "DIR-" + str(uuid.uuid4())[:6].upper()

    # Générer les identifiants
    reserved = _collect_reserved_usernames()
    username, password = _generate_unique_credentials(name, reserved)

    # Enregistrer dans access_control (hash bcrypt)
    access = _db.get_access_control(default_users=[], default_sales_mode="open")
    if not isinstance(access, dict):
        access = {}
    users = access.get("users")
    if not isinstance(users, list):
        users = []
    hashed_pw = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    users.append({
        "username": username,
        "password": hashed_pw,
        "role": "client",
        "active": True,
        "paid_comparison_euros": 0,
        "plan": "essai",
        "email": email,
        "name": name,
        "created_at": now_iso,
        "sub_id": sub_id,
        "trial_end": trial_end,
    })
    access["users"] = users
    if not _db.save_access_control(access):
        raise ValueError("Écriture access_control impossible")

    sub: dict[str, object] = {
        "id": sub_id,
        "name": name,
        "email": email,
        "plan": "essai",
        "amount": 0,
        "reference": "",
        "submitted_at": now_iso,
        "validated_at": now_iso,
        "status": "trial",
        "username": username,
        "password": password,  # clair — pour affichage WA, non persisté durablement
        "trial_start": now_iso,
        "trial_end": trial_end,
        "trial_days": days,
        "personal_message": (personal_message or "").strip(),
        "source": "direct_admin",
    }

    # Persister le sub dans subscriptions
    data = _load_subs()
    validated = list(data.get("validated") or [])
    validated.append(sub)
    data["validated"] = validated
    _save_subs(data)

    # Email premium
    if email:
        try:
            send_premium_welcome_email(sub, personal_message=personal_message or "")
        except Exception as e:
            logger.warning("Email bienvenue: %s", e)

    logger.info("Accès direct créé : %s (%s) — %d jours", username, email, days)
    return sub


def send_premium_welcome_email(
    sub: dict[str, object],
    *,
    personal_message: str = "",
) -> None:
    """
    Email premium personnalisé : ton chaleureux, mot personnel, identifiants clairs.
    """
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_pass = os.getenv("SMTP_PASS", "")

    to_addr = str(sub.get("email") or "").strip()
    if not smtp_user or not smtp_pass or not to_addr:
        logger.warning("SMTP non configuré ou email absent — email premium non envoyé")
        return

    prenom = str((sub.get("name") or "").split()[0] or "").strip() or "vous"
    prenom_cap = prenom.capitalize()
    username = str(sub.get("username") or "")
    password = str(sub.get("password") or "")
    days = int(sub.get("trial_days") or 7)
    trial_end_raw = str(sub.get("trial_end") or "")
    public_url = public_base_url()

    # Formater la date d'expiration lisiblement
    exp_label = trial_end_raw
    try:
        exp_dt = datetime.fromisoformat(trial_end_raw.replace("Z", "+00:00"))
        exp_label = exp_dt.strftime("%-d %B %Y à %H:%M UTC")
    except Exception:
        pass

    # Bloc mot personnel (optionnel)
    personal_block = ""
    pm = (personal_message or "").strip()
    if pm:
        safe_pm = escape(pm)
        personal_block = f"""
        <div style="margin:20px 0;padding:14px 18px;background:#0f1f0f;border-left:3px solid #00ff88;border-radius:0 8px 8px 0">
          <p style="margin:0;color:#ccc;font-style:italic;font-size:.95rem">"{safe_pm}"</p>
        </div>"""

    duration_text = f"{days} jour{'s' if days > 1 else ''}"

    safe_user = escape(username)
    safe_pw = escape(password)
    safe_url = escape(public_url, quote=True)

    html = f"""<!DOCTYPE html>
<html lang="fr">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#0a0a0a;font-family:ui-sans-serif,system-ui,-apple-system,sans-serif">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#0a0a0a;padding:40px 16px">
    <tr><td align="center">
      <table width="100%" style="max-width:560px;background:#111;border:1px solid #1f1f1f;border-radius:16px;overflow:hidden">

        <!-- Header -->
        <tr><td style="background:linear-gradient(135deg,#052818 0%,#0a3d1a 100%);padding:32px 32px 24px;text-align:center">
          <p style="margin:0 0 6px;font-size:1.6rem;font-weight:900;color:#00ff88;letter-spacing:-.02em">👟 SneakerBot</p>
          <p style="margin:0;color:#6ee7b7;font-size:.9rem;font-weight:500">Votre outil d'analyse de prix sneakers</p>
        </td></tr>

        <!-- Body -->
        <tr><td style="padding:32px">
          <p style="margin:0 0 16px;font-size:1.05rem;color:#f4f4f5">Bonjour <strong style="color:#00ff88">{prenom_cap}</strong>,</p>
          <p style="margin:0 0 14px;color:#d1d5db;line-height:1.6">
            Nous sommes vraiment heureux de vous accueillir sur SneakerBot.<br>
            Votre accès découverte est prêt — voici tout ce dont vous avez besoin pour démarrer.
          </p>
          {personal_block}

          <!-- Accès card -->
          <div style="background:#0d0d0d;border:1px solid #2a2a2a;border-radius:12px;padding:20px;margin:20px 0">
            <p style="margin:0 0 14px;font-size:.75rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#6b7280">Vos identifiants personnels</p>
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td style="padding:8px 0;color:#9ca3af;font-size:.88rem;width:130px">🔗 Plateforme</td>
                <td style="padding:8px 0"><a href="{safe_url}" style="color:#00ff88;text-decoration:none;font-weight:600">{safe_url}</a></td>
              </tr>
              <tr>
                <td style="padding:8px 0;color:#9ca3af;font-size:.88rem">👤 Identifiant</td>
                <td style="padding:8px 0"><code style="background:#1a1a1a;color:#00ff88;padding:3px 10px;border-radius:6px;font-size:.9rem;font-weight:700">{safe_user}</code></td>
              </tr>
              <tr>
                <td style="padding:8px 0;color:#9ca3af;font-size:.88rem">🔑 Mot de passe</td>
                <td style="padding:8px 0"><code style="background:#1a1a1a;color:#00ff88;padding:3px 10px;border-radius:6px;font-size:.9rem;font-weight:700">{safe_pw}</code></td>
              </tr>
            </table>
          </div>

          <!-- Durée -->
          <div style="display:flex;gap:12px;margin:0 0 24px">
            <div style="flex:1;background:#0d0d0d;border:1px solid #2a2a2a;border-radius:10px;padding:14px">
              <p style="margin:0 0 4px;font-size:.72rem;color:#6b7280;text-transform:uppercase;letter-spacing:.06em">Durée offerte</p>
              <p style="margin:0;font-size:1.1rem;font-weight:800;color:#00ff88">{duration_text}</p>
            </div>
            <div style="flex:1;background:#0d0d0d;border:1px solid #2a2a2a;border-radius:10px;padding:14px">
              <p style="margin:0 0 4px;font-size:.72rem;color:#6b7280;text-transform:uppercase;letter-spacing:.06em">Accès jusqu'au</p>
              <p style="margin:0;font-size:.88rem;font-weight:700;color:#d1d5db">{exp_label}</p>
            </div>
          </div>

          <!-- CTA -->
          <div style="text-align:center;margin:28px 0 20px">
            <a href="{safe_url}/login"
               style="display:inline-block;background:#00ff88;color:#052818;padding:14px 36px;border-radius:10px;font-weight:900;font-size:1rem;text-decoration:none;letter-spacing:-.01em">
              🚀 Accéder à mon espace
            </a>
          </div>

          <p style="margin:0;color:#6b7280;font-size:.82rem;line-height:1.6;text-align:center">
            Une question ? Répondez directement à cet email, je suis disponible.<br>
            Bonne analyse 👟
          </p>
        </td></tr>

        <!-- Footer -->
        <tr><td style="background:#0a0a0a;padding:16px 32px;border-top:1px solid #1f1f1f;text-align:center">
          <p style="margin:0;color:#4b5563;font-size:.75rem">SneakerBot · Analyse de prix sneakers FR · <a href="{safe_url}" style="color:#374151">{safe_url}</a></p>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"👟 Votre accès SneakerBot est prêt, {prenom_cap} !"
    msg["From"] = f"SneakerBot <{smtp_user}>"
    msg["To"] = to_addr
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, to_addr, msg.as_string())

    logger.info("Email premium envoyé à %s (%s)", to_addr, prenom_cap)


def build_whatsapp_client_access_url(
    sub: dict[str, object],
    *,
    personal_message: str = "",
    client_phone: str = "",
) -> str:
    """
    Construit un lien wa.me prêt à envoyer au client avec ses identifiants.
    Si client_phone est fourni, le lien pointe directement vers ce numéro.
    Sinon, redirige via le numéro admin (copier/coller pour l'admin).
    """
    prenom = str((sub.get("name") or "").split()[0] or "").strip().capitalize()
    base_url = public_base_url()
    username = str(sub.get("username") or "")
    password = str(sub.get("password") or "")
    days = int(sub.get("trial_days") or 7)

    pm = (personal_message or sub.get("personal_message") or "").strip()
    pm_block = f"\n\n💬 {pm}" if pm else ""

    duration_text = f"{days} jour{'s' if days > 1 else ''}"

    message = (
        f"Bonjour {prenom} 👋\n\n"
        f"Votre accès SneakerBot est prêt !{pm_block}\n\n"
        f"🔗 {base_url}\n"
        f"👤 Login : {username}\n"
        f"🔑 Pass : {password}\n\n"
        f"⏱ Accès découverte : {duration_text}\n\n"
        f"Connectez-vous ici :\n{base_url}/login\n\n"
        f"N'hésitez pas à me contacter si vous avez des questions 👟"
    )

    number = "".join(c for c in (client_phone or get_admin_whatsapp_digits()) if c.isdigit())
    return f"https://wa.me/{number}?text={quote(message, safe='')}"


def suspend_user(username: str) -> None:
    """Désactive un compte utilisateur (active=False)."""
    access = _db.get_access_control(default_users=[], default_sales_mode="open")
    if not isinstance(access, dict):
        raise ValueError("access_control illisible")
    users = access.get("users")
    if not isinstance(users, list):
        raise ValueError("Aucun utilisateur")
    found = False
    for u in users:
        if isinstance(u, dict) and u.get("username") == username:
            u["active"] = False
            u["suspended_at"] = datetime.now(timezone.utc).isoformat()
            found = True
            break
    if not found:
        raise ValueError(f"Utilisateur {username!r} introuvable")
    if not _db.save_access_control(access):
        raise ValueError("Écriture access_control impossible")
    logger.info("Compte suspendu : %s", username)


def reactivate_user(username: str) -> None:
    """Réactive un compte utilisateur suspendu (active=True)."""
    access = _db.get_access_control(default_users=[], default_sales_mode="open")
    if not isinstance(access, dict):
        raise ValueError("access_control illisible")
    users = access.get("users")
    if not isinstance(users, list):
        raise ValueError("Aucun utilisateur")
    found = False
    for u in users:
        if isinstance(u, dict) and u.get("username") == username:
            u["active"] = True
            u.pop("suspended_at", None)
            u["reactivated_at"] = datetime.now(timezone.utc).isoformat()
            found = True
            break
    if not found:
        raise ValueError(f"Utilisateur {username!r} introuvable")
    if not _db.save_access_control(access):
        raise ValueError("Écriture access_control impossible")
    logger.info("Compte réactivé : %s", username)


def extend_trial(sub_id: str, *, days: int) -> dict[str, object]:
    """
    Prolonge un essai existant de N jours supplémentaires.
    Met à jour trial_end dans subscriptions ET access_control.
    """
    days = max(1, min(int(days), 3650))
    data = _load_subs()
    validated = [s for s in (data.get("validated") or []) if isinstance(s, dict)]
    sub = next((s for s in validated if str(s.get("id")) == str(sub_id)), None)
    if not sub:
        raise ValueError(f"Abonnement {sub_id} introuvable")

    # Calculer la nouvelle date d'expiration (depuis l'expiration actuelle ou maintenant)
    old_end_raw = str(sub.get("trial_end") or "")
    try:
        old_end = datetime.fromisoformat(old_end_raw.replace("Z", "+00:00"))
        base = max(old_end, datetime.now(timezone.utc))
    except Exception:
        base = datetime.now(timezone.utc)
    new_end = (base + timedelta(days=days)).isoformat()

    sub = dict(sub)
    sub["trial_end"] = new_end
    sub["trial_days"] = int(sub.get("trial_days") or 0) + days

    # Mise à jour dans subscriptions
    data["validated"] = [
        dict(s) if str(s.get("id")) != str(sub_id) else sub
        for s in validated
    ]
    _save_subs(data)

    # Mise à jour dans access_control
    username = str(sub.get("username") or "")
    if username:
        access = _db.get_access_control(default_users=[], default_sales_mode="open")
        if isinstance(access, dict):
            users = access.get("users")
            if isinstance(users, list):
                for u in users:
                    if isinstance(u, dict) and u.get("username") == username:
                        u["trial_end"] = new_end
                        u["active"] = True
                        break
                if not _db.save_access_control(access):
                    logger.warning("extend_trial: écriture access_control impossible")

    logger.info("Essai prolongé : %s (%s) +%d jours → %s", sub_id, username, days, new_end)
    return sub
