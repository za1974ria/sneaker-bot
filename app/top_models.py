"""
TOP 30 — Modèles à traitement premium SneakerBot.

Source unique de vérité pour toute la codebase.
Utiliser is_top_model() partout, ne jamais dupliquer la liste.
"""

from __future__ import annotations

import unicodedata


def _norm(s: str) -> str:
    """Normalise brand/model pour comparaison insensible à la casse et aux accents."""
    nfkd = unicodedata.normalize("NFKD", s.strip().lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


# ── TOP 30 : les modèles les plus demandés sur le marché FR 2026 ───────────
# Sélection : iconicité marque × volume demande × trend 2024-2026
# Format : (brand_norm, model_norm) — les deux normalisés via _norm()
_TOP_30_RAW: tuple[tuple[str, str], ...] = (
    # ── Nike (9) ──────────────────────────────────────────────────────────
    ("nike", "air force 1 low"),
    ("nike", "dunk low retro"),
    ("nike", "air jordan 1 low"),
    ("nike", "air jordan 1 mid"),
    ("nike", "air max 90 essential"),
    ("nike", "air max 95 essential"),
    ("nike", "dunk low women"),
    ("nike", "air force 1 low women"),
    ("nike", "zoom vomero 5 women"),
    # ── Adidas (7) ────────────────────────────────────────────────────────
    ("adidas", "samba og"),
    ("adidas", "gazelle indoor"),
    ("adidas", "campus 00s"),
    ("adidas", "stan smith"),
    ("adidas", "samba og women"),
    ("adidas", "gazelle bold women"),
    ("adidas", "campus 00s women"),
    # ── New Balance (5) ───────────────────────────────────────────────────
    ("new balance", "550 white green"),
    ("new balance", "574 core"),
    ("new balance", "530 white"),
    ("new balance", "990v6 made in usa"),
    ("new balance", "550 women"),
    # ── Asics (3) ─────────────────────────────────────────────────────────
    ("asics", "gel-1130"),
    ("asics", "gt-2160"),
    ("asics", "gel-kayano 14"),
    # ── Salomon (2) ───────────────────────────────────────────────────────
    ("salomon", "xt-4 og"),
    ("salomon", "xt-6 advanced"),
    # ── Mono-marque (4) ───────────────────────────────────────────────────
    ("converse", "chuck 70 hi"),
    ("puma", "speedcat og"),
    ("on running", "cloud 5 waterproof"),
    ("vans", "old skool overt"),
)

TOP_30: frozenset[tuple[str, str]] = frozenset(
    (_norm(b), _norm(m)) for b, m in _TOP_30_RAW
)

# Paramètres de filtrage premium (médiane robuste plus stricte que standard)
TOP_30_MEDIAN_LOW = 0.60   # rejette prix < 60 % médiane (vs 55 % standard)
TOP_30_MEDIAN_HIGH = 1.45  # rejette prix > 145 % médiane (vs 165 % standard)
TOP_30_MIN_SOURCES = 5     # seuil "sources suffisantes" pour confiance élevée
TOP_30_CONFIDENCE_BONUS = 10  # pts bonus confidence_score si top modèle bien alimenté


def is_top_model(brand: str, model: str) -> bool:
    """True si (brand, model) fait partie du TOP 30."""
    return (_norm(brand), _norm(model)) in TOP_30


def top_model_tier(brand: str, model: str) -> str:
    """Retourne 'premium' pour les TOP 30, 'standard' pour les autres."""
    return "premium" if is_top_model(brand, model) else "standard"


def tier_badge_label(brand: str, model: str, confidence_score: int = 0) -> str:
    """Retourne 'OR' / 'ARGENT' / 'BRONZE' selon positionnement business.

    OR     = TOP 30 (modèles les plus demandés, traitement premium)
    ARGENT = score confiance ≥ 80 (données fiables, niveau propre standard+)
    BRONZE = score confiance < 80 (données correctes, niveau cohérent simple)
    """
    if is_top_model(brand, model):
        return "OR"
    if confidence_score >= 80:
        return "ARGENT"
    return "BRONZE"
