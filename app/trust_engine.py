"""
SneakerBot Trust Engine — Prix fiables et explicables.

Fonctions publiques :
  normalize_shop_name(name)          -> str
  get_source_weight(shop_name)       -> float   (0.20 – 1.00)
  classify_shop_tier(shop_name)      -> str
  detect_source_anomalies(sources)   -> list[dict]
  compute_trusted_prices(sources)    -> dict
  build_trust_report(brand, model, sources, market_meta) -> dict

Conçu pour être appelé depuis _api_comparison_fr_impl — zéro dépendance
sur les autres modules app.*, aucun appel HTTP, aucune écriture disque.
Fallback neutre si données insuffisantes (score=60, label="données limitées").
"""

from __future__ import annotations

import re
import statistics
import unicodedata
from typing import Any

# ──────────────────────────────────────────────────────────────────────────────
# 1. CLASSIFICATION DES BOUTIQUES PAR NIVEAU DE CONFIANCE
# ──────────────────────────────────────────────────────────────────────────────

# Sites officiels marque (poids 1.00)
_OFFICIAL_BRAND_SITES: frozenset[str] = frozenset({
    "nike fr", "nike officiel", "adidas.fr", "adidas fr", "new balance",
    "puma", "puma.com", "reebok", "salomon", "asics", "vans", "converse",
    "on", "on running", "new balance fr", "jordan", "under armour",
    "timberland", "dr. martens", "birkenstock", "crocs", "ugg",
    "superga", "superga.com", "steve madden france", "toms.com/fr-fr",
    "buffalo-boots.com", "mac douglas",
})

# Grands retailers reconnus FR (poids 0.90)
_MAJOR_RETAILERS: frozenset[str] = frozenset({
    "zalando", "zalando.fr", "footlocker.fr", "jd sports france",
    "intersport", "courir", "sarenza.com", "asos", "fnac", "decathlon",
    "galeries lafayette", "printemps.com", "la redoute", "kiabi.com",
    "spartoo", "spartoo.com", "foot locker", "sport 2000", "size? france",
    "sportsdirect.fr", "go sport", "snipes.com", "aboutyou.fr",
    "place des tendances", "showroomprive", "manfield", "mytheresa",
    "free people fr", "ikks",
})

# Retailers spécialisés sneakers fiables (poids 0.82)
_SNEAKER_SPECIALISTS: frozenset[str] = frozenset({
    "solebox", "overkill", "bstn store", "size factory", "wethenew",
    "basket4ballers", "afew store", "asphaltgold", "footdistrict",
    "footdistrict fr", "pro:direct sport fr", "sneak boutik",
    "basketball emotion fr", "unisportstore.fr", "kicks machine",
    "kickscrew", "kicksusa", "sneakersnstuff", "limited resell",
    "hypeboost", "hype shoes", "hype cult", "novelship", "laced",
    "offshoes", "skatedeluxe.fr", "overkill shop", "eatsnkrs",
    "espace des marques", "corner street", "snipes",
    "private sport shop", "sportokay.com", "hardloop", "alltricks",
    "snowleader.com", "passion-running.fr", "fun-sport-vision",
    "sport direct", "sportega.fr", "athletiko", "mec shopping",
    "la bonne pointure", "shooos.fr", "shoes.fr", "niceshoes.fr",
    "pegashoes", "modz",
})

# Marketplaces fiables (poids 0.75)
_RELIABLE_MARKETPLACES: frozenset[str] = frozenset({
    "amazon.fr", "amazon.fr - seller", "rakuten - mode - occasion",
    "rakuten - mmzci", "stockx", "farfetch.com", "yoox", "vestiaire collective",
    "miinto.fr", "ebay - brandstyle24", "ebay - scorpionshoes-uk",
    "vertbaudet marketplace", "boulanger - boulanger marketplace",
    "kaufland.fr - takemore", "joom", "ubuy", "outlet46.de",
    "outlet pc fr", "preloved france", "leboncoin", "mimanera",
    "footasylum", "fanatics fr", "kitbag eu",
})

# Patterns suspects : resell douteux, destinations lointaines, noms peu connus
_SUSPECT_PATTERNS: list[str] = [
    r"resell",
    r"côte d.ivoire",
    r"secondstep",
    r"madonnina",
    r"suffern",
    r"babunkers",
    r"beebs",
    r"pikastore",
    r"seoulterrace",
    r"topperzstore",
    r"solem8te",
    r"outletsanmichele",
    r"hylton",
    r"instinctpremium",
]

_SUSPECT_RE = re.compile("|".join(_SUSPECT_PATTERNS), re.IGNORECASE)

# ──────────────────────────────────────────────────────────────────────────────
# 2. FONCTIONS PUBLIQUES — SHOP WEIGHTING
# ──────────────────────────────────────────────────────────────────────────────

def normalize_shop_name(name: str) -> str:
    """Normalise un nom de boutique : lowercase, strip, accents retirés."""
    if not name:
        return ""
    n = name.strip().lower()
    # Retirer accents pour matching robuste
    n = "".join(
        c for c in unicodedata.normalize("NFD", n)
        if unicodedata.category(c) != "Mn"
    )
    # Uniformiser les espaces multiples
    n = re.sub(r"\s+", " ", n)
    return n


def classify_shop_tier(shop_name: str) -> str:
    """
    Retourne le tier d'une boutique :
      'official' | 'major_retailer' | 'specialist' | 'marketplace' | 'unknown' | 'suspect'
    """
    if not shop_name:
        return "unknown"
    n = normalize_shop_name(shop_name)
    if _SUSPECT_RE.search(n):
        return "suspect"
    if n in _OFFICIAL_BRAND_SITES or any(n.startswith(s) for s in _OFFICIAL_BRAND_SITES):
        return "official"
    if n in _MAJOR_RETAILERS:
        return "major_retailer"
    if n in _SNEAKER_SPECIALISTS:
        return "specialist"
    if n in _RELIABLE_MARKETPLACES:
        return "marketplace"
    # Heuristiques supplémentaires
    if any(kw in n for kw in (".fr", "france", "officiel", "official")):
        # Site .fr ou mention officielle → légèrement plus fiable
        return "marketplace"
    return "unknown"


def get_source_weight(shop_name: str) -> float:
    """
    Retourne le poids de confiance d'une boutique (0.20 – 1.00).

    Barème :
      official        → 1.00  (site marque)
      major_retailer  → 0.90  (Zalando, Foot Locker…)
      specialist      → 0.82  (Solebox, Basket4Ballers…)
      marketplace     → 0.75  (Amazon, StockX…)
      unknown         → 0.50  (boutique non référencée)
      suspect         → 0.20  (resell douteux, géo suspecte)
    """
    tier = classify_shop_tier(shop_name)
    return {
        "official":       1.00,
        "major_retailer": 0.90,
        "specialist":     0.82,
        "marketplace":    0.75,
        "unknown":        0.50,
        "suspect":        0.20,
    }[tier]


# ──────────────────────────────────────────────────────────────────────────────
# 3. BRAND PRICE FLOORS / CEILINGS (copie légère — pas d'import circulaire)
# ──────────────────────────────────────────────────────────────────────────────

_BRAND_FLOOR: dict[str, float] = {
    "Nike": 55.0, "Adidas": 45.0, "New Balance": 55.0,
    "Salomon": 90.0, "Asics": 55.0, "Puma": 40.0,
    "Reebok": 40.0, "Vans": 45.0, "Converse": 40.0,
    "On Running": 90.0, "On": 90.0,
}
_BRAND_CEIL: dict[str, float] = {
    "Nike": 350.0, "Adidas": 280.0, "New Balance": 320.0,
    "Salomon": 350.0, "Asics": 300.0, "Puma": 220.0,
    "Reebok": 220.0, "Vans": 180.0, "Converse": 180.0,
    "On Running": 320.0, "On": 320.0,
}
_DEFAULT_FLOOR = 25.0
_DEFAULT_CEIL  = 500.0


def _brand_bounds(brand: str, model: str) -> tuple[float, float]:
    lo = _BRAND_FLOOR.get(brand, _DEFAULT_FLOOR)
    hi = _BRAND_CEIL.get(brand, _DEFAULT_CEIL)
    ml = (model or "").lower()
    if any(k in ml for k in ("990", "made in usa", "made in uk", "gore-tex", "collab", "limited")):
        hi = hi * 1.30
    if any(k in ml for k in ("women", "femme", "wmns")):
        lo = max(20.0, lo - 10.0)
    return lo, hi


# ──────────────────────────────────────────────────────────────────────────────
# 4. DÉTECTION D'ANOMALIES PAR SOURCE
# ──────────────────────────────────────────────────────────────────────────────

def detect_source_anomalies(
    sources: list[dict[str, Any]],
    brand: str = "",
    model: str = "",
) -> list[dict[str, Any]]:
    """
    Prend la liste des sources brutes (dicts avec au moins 'price_avg', 'shop').
    Retourne la même liste avec un champ 'anomaly' ajouté à chaque source :
      {
        "is_anomaly": bool,
        "reason":     str,    # "" si normal
        "severity":   str,    # "" | "low" | "high"
        "excluded_from_trusted": bool,
      }
    """
    prices = [float(s.get("price_avg") or 0) for s in sources if float(s.get("price_avg") or 0) > 0]
    floor, ceil_price = _brand_bounds(brand, model)

    # IQR sur les prix valides (brand bounds)
    in_range = [p for p in prices if floor <= p <= ceil_price]
    working = in_range if len(in_range) >= 2 else prices

    median_price: float | None = None
    q1: float | None = None
    q3: float | None = None
    lo_fence: float | None = None
    hi_fence: float | None = None

    if len(working) >= 2:
        median_price = statistics.median(working)
        if len(working) >= 4:
            try:
                q1v, _, q3v = statistics.quantiles(sorted(working), n=4, method="inclusive")
            except TypeError:
                q1v, _, q3v = statistics.quantiles(sorted(working), n=4)
            q1, q3 = float(q1v), float(q3v)
            iqr = q3 - q1
            lo_fence = q1 - 1.5 * iqr
            hi_fence = q3 + 1.5 * iqr
        else:
            q1 = float(min(working))
            q3 = float(max(working))
            lo_fence = q1
            hi_fence = q3
    elif len(working) == 1:
        median_price = working[0]

    annotated: list[dict[str, Any]] = []
    for src in sources:
        s = dict(src)
        price = float(s.get("price_avg") or 0)
        shop = str(s.get("shop") or "")
        weight = get_source_weight(shop)
        tier = classify_shop_tier(shop)

        is_anomaly = False
        reasons: list[str] = []
        severity = ""
        excluded = False

        if price <= 0:
            is_anomaly = True
            reasons.append("prix_nul")
            severity = "high"
            excluded = True
        else:
            # Hors plafond/plancher marque
            if price < floor:
                is_anomaly = True
                reasons.append("sous_plancher_marque")
                severity = "high"
                excluded = True
            elif price > ceil_price:
                is_anomaly = True
                reasons.append("dessus_plafond_marque")
                severity = "low"
                excluded = False  # sur-prix possible (premium, limited)

            # Fake Low Price Killer — seuil strict :
            # Les shop_items sont déjà pré-filtrés par le comparison pipeline.
            # On exclut les prix < 35% de la médiane (fraude/dropship évidente).
            if median_price and median_price > 0:
                ratio = price / median_price
                if ratio < 0.35:
                    is_anomaly = True
                    reasons.append("prix_trop_bas_vs_mediane")
                    severity = "high"
                    excluded = True
                elif ratio < 0.55:
                    # Flagué (suspect) mais pas automatiquement exclu
                    is_anomaly = True
                    reasons.append("prix_suspect_bas")
                    severity = "high"
                    # Exclure si source inconnue ou suspecte
                    if tier in ("unknown", "suspect"):
                        excluded = True
                elif ratio < 0.65:
                    # Flagué (potentiellement promo/déstockage) mais PAS exclu
                    is_anomaly = True
                    reasons.append("prix_bas_vs_mediane")
                    severity = severity or "low"
                elif ratio > 2.00:
                    reasons.append("prix_tres_haut_vs_mediane")
                    if not is_anomaly:
                        is_anomaly = True
                        severity = "low"

            # IQR outlier
            if lo_fence is not None and price < lo_fence and price >= floor:
                if "prix_trop_bas_vs_mediane" not in reasons:
                    is_anomaly = True
                    reasons.append("iqr_low_outlier")
                    severity = severity or "low"
                    excluded = excluded or True
            if hi_fence is not None and price > hi_fence and price <= ceil_price:
                if "prix_tres_haut_vs_mediane" not in reasons:
                    is_anomaly = True
                    reasons.append("iqr_high_outlier")
                    severity = severity or "low"

            # Boutique suspecte : flaguer, exclure si prix même légèrement atypique
            if tier == "suspect":
                is_anomaly = True
                reasons.append("source_suspecte")
                severity = "high"
                # Exclure les suspects si prix < 85% médiane ou > 180% médiane
                if not median_price or price < median_price * 0.85 or price > median_price * 1.80:
                    excluded = True
            elif tier == "unknown" and median_price and price < median_price * 0.65:
                is_anomaly = True
                reasons.append("source_inconnue_prix_tres_bas")
                severity = severity or "low"
                excluded = True

        s["anomaly"] = {
            "is_anomaly": is_anomaly,
            "reason": ", ".join(reasons) if reasons else "",
            "severity": severity,
            "excluded_from_trusted": excluded,
        }
        s["source_weight"] = weight
        s["source_tier"] = tier
        annotated.append(s)

    return annotated


# ──────────────────────────────────────────────────────────────────────────────
# 5. CALCUL DES PRIX FIABLES PONDÉRÉS
# ──────────────────────────────────────────────────────────────────────────────

def compute_trusted_prices(
    sources: list[dict[str, Any]],
    brand: str = "",
    model: str = "",
) -> dict[str, Any]:
    """
    Calcule les prix fiables pondérés par le poids des sources.
    Exclut les anomalies marquées 'excluded_from_trusted'.

    Retourne :
    {
        trusted_min_price:    float | None,
        trusted_avg_price:    float | None,
        trusted_median_price: float | None,
        trusted_sources_count: int,
        excluded_anomalies_count: int,
        total_sources_count: int,
        weighted_avg: float | None,
    }
    """
    # S'assurer que les anomalies sont détectées
    annotated = detect_source_anomalies(sources, brand, model)

    trusted: list[tuple[float, float]] = []   # (price, weight)
    excluded_count = 0

    for src in annotated:
        price = float(src.get("price_avg") or 0)
        anom = src.get("anomaly", {})
        if price <= 0:
            continue
        if anom.get("excluded_from_trusted"):
            excluded_count += 1
            continue
        w = float(src.get("source_weight") or 0.50)
        trusted.append((price, w))

    if not trusted:
        return {
            "trusted_min_price":      None,
            "trusted_avg_price":      None,
            "trusted_median_price":   None,
            "trusted_sources_count":  0,
            "excluded_anomalies_count": excluded_count,
            "total_sources_count":    len(annotated),
            "weighted_avg":           None,
            "data_limited":           True,
        }

    prices_only = [p for p, _ in trusted]
    weights = [w for _, w in trusted]
    total_w = sum(weights) or 1.0

    weighted_avg = sum(p * w for p, w in trusted) / total_w
    simple_median = statistics.median(prices_only)

    return {
        "trusted_min_price":      round(min(prices_only), 2),
        "trusted_avg_price":      round(sum(prices_only) / len(prices_only), 2),
        "trusted_median_price":   round(simple_median, 2),
        "trusted_sources_count":  len(trusted),
        "excluded_anomalies_count": excluded_count,
        "total_sources_count":    len(annotated),
        "weighted_avg":           round(weighted_avg, 2),
        "data_limited":           len(trusted) < 2,
    }


# ──────────────────────────────────────────────────────────────────────────────
# 6. RAPPORT COMPLET D'EXPLAINABILITÉ
# ──────────────────────────────────────────────────────────────────────────────

def build_trust_report(
    brand: str,
    model: str,
    sources: list[dict[str, Any]],
    market_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Construit le rapport complet de confiance pour un modèle.

    Paramètre `sources` : liste de dicts source (shop_items du comparison impl).
    Paramètre `market_meta` : dict optionnel (ex: confidence_score déjà calculé).

    Retourne un dict `trust_report` prêt à être sérialisé en JSON.
    """
    if not sources:
        return _neutral_report(brand, model)

    try:
        annotated = detect_source_anomalies(sources, brand, model)
        tp = compute_trusted_prices(sources, brand, model)

        # ── Compter les tiers ──────────────────────────────────────────────
        tier_counts: dict[str, int] = {}
        premium_shop_names: list[str] = []
        for src in annotated:
            tier = src.get("source_tier", "unknown")
            tier_counts[tier] = tier_counts.get(tier, 0) + 1
            shop = str(src.get("shop") or "")
            if tier in ("official", "major_retailer") and not src.get("anomaly", {}).get("excluded_from_trusted"):
                if shop and shop not in premium_shop_names:
                    premium_shop_names.append(shop)

        premium_sources_count = tier_counts.get("official", 0) + tier_counts.get("major_retailer", 0)
        distinct_shops_count = len({str(s.get("shop") or "") for s in annotated if s.get("shop")})

        # ── Calcul du spread de prix (dispersion relative) ─────────────────
        all_prices = [float(s.get("price_avg") or 0) for s in sources if float(s.get("price_avg") or 0) > 0]
        if len(all_prices) >= 2:
            price_min = min(all_prices)
            price_max = max(all_prices)
            price_avg = sum(all_prices) / len(all_prices)
            spread_pct = round((price_max - price_min) / price_avg * 100, 1) if price_avg > 0 else 0.0
        else:
            spread_pct = 0.0

        # ── Score confiance Trust Engine ───────────────────────────────────
        # Utilise le score existant si fourni, sinon calcule un score léger.
        existing_score = int((market_meta or {}).get("confidence_score") or 0)
        trust_score = _compute_trust_score(
            trusted_sources_count=tp["trusted_sources_count"],
            excluded_count=tp["excluded_anomalies_count"],
            premium_count=premium_sources_count,
            distinct_shops=distinct_shops_count,
            data_limited=tp.get("data_limited", False),
            existing_score=existing_score,
            spread_pct=spread_pct,
        )

        # ── Raison textuelle (explainability) ─────────────────────────────
        explanation = _build_explanation(
            score=trust_score["score"],
            trusted_count=tp["trusted_sources_count"],
            excluded_count=tp["excluded_anomalies_count"],
            premium_count=premium_sources_count,
            distinct_shops=distinct_shops_count,
            data_limited=tp.get("data_limited", False),
        )

        # ── Sources anomales (pour trace côté client) ──────────────────────
        anomaly_details: list[dict[str, Any]] = []
        for src in annotated:
            anom = src.get("anomaly", {})
            if anom.get("is_anomaly"):
                anomaly_details.append({
                    "shop": str(src.get("shop") or ""),
                    "price": float(src.get("price_avg") or 0),
                    "reason": anom.get("reason", ""),
                    "severity": anom.get("severity", ""),
                    "excluded": anom.get("excluded_from_trusted", False),
                })

        return {
            # Prix fiables
            "trusted_min_price":      tp["trusted_min_price"],
            "trusted_avg_price":      tp["trusted_avg_price"],
            "trusted_median_price":   tp["trusted_median_price"],
            "weighted_avg_price":     tp["weighted_avg"],
            # Métriques sources
            "trusted_sources_count":  tp["trusted_sources_count"],
            "excluded_anomalies_count": tp["excluded_anomalies_count"],
            "total_sources_count":    tp["total_sources_count"],
            "premium_sources_count":  premium_sources_count,
            "distinct_shops_count":   distinct_shops_count,
            "premium_shop_names":     premium_shop_names[:8],  # max 8
            # Score confiance
            "trust_score":            trust_score["score"],
            "trust_label":            trust_score["label"],
            "trust_color":            trust_score["color"],
            # Prix spread
            "price_spread_pct":       spread_pct,
            # Explainability
            "explanation":            explanation,
            "anomaly_details":        anomaly_details[:10],  # max 10
            "data_limited":           tp.get("data_limited", False),
            # Tier breakdown
            "tier_counts":            tier_counts,
        }

    except Exception:
        return _neutral_report(brand, model)


# ──────────────────────────────────────────────────────────────────────────────
# 7. HELPERS INTERNES
# ──────────────────────────────────────────────────────────────────────────────

def _compute_trust_score(
    trusted_sources_count: int,
    excluded_count: int,
    premium_count: int,
    distinct_shops: int,
    data_limited: bool,
    existing_score: int = 0,
    spread_pct: float = 0.0,
) -> dict[str, Any]:
    """Score 0-100 basé sur les critères du Trust Engine."""
    if data_limited and trusted_sources_count == 0:
        return {"score": 60, "label": "Données limitées", "color": "orange"}

    score = 40  # base

    # Sources fiables
    if trusted_sources_count >= 10:
        score += 25
    elif trusted_sources_count >= 5:
        score += 18
    elif trusted_sources_count >= 3:
        score += 12
    elif trusted_sources_count >= 2:
        score += 6
    elif trusted_sources_count == 1:
        score += 2

    # Sources premium (boutiques officielles + grands retailers)
    if premium_count >= 3:
        score += 20
    elif premium_count >= 2:
        score += 14
    elif premium_count == 1:
        score += 8

    # Diversité des boutiques
    if distinct_shops >= 8:
        score += 10
    elif distinct_shops >= 5:
        score += 6
    elif distinct_shops >= 3:
        score += 3

    # Pénalité anomalies exclues (par rapport au total)
    total = trusted_sources_count + excluded_count
    if total > 0:
        anomaly_ratio = excluded_count / total
        if anomaly_ratio > 0.50:
            score -= 15
        elif anomaly_ratio > 0.30:
            score -= 8
        elif anomaly_ratio > 0.15:
            score -= 3

    # Intégration score existant (confiance_scorer) comme signal principal
    if existing_score > 0:
        # trust_engine affine le confidence_scorer existant (+/- 15 pts max)
        # Le scorer existant porte freshness, spread, top30, brand rules : on s'y fie.
        delta = score - 65  # delta par rapport à la base neutre 65
        delta = max(-15, min(15, delta))
        score = round(existing_score + delta * 0.50)
    # Plancher : avec ≥1 source validée et aucune exclusion, on ne descend pas sous 55
    if trusted_sources_count >= 1 and excluded_count == 0:
        score = max(score, 55)

    score = max(0, min(100, score))

    # ── Pénalité spread de prix — dispersion élevée = moins fiable ─────────────
    # Un grand spread indique des prix très disparates entre sources → moins fiable.
    if spread_pct >= 90:
        score -= 20   # Dispersion extrême (Forum High ~961%)
    elif spread_pct >= 70:
        score -= 12   # Dispersion forte
    elif spread_pct >= 50:
        score -= 7    # Dispersion notable (Campus 00s ~124%)

    score = max(0, min(100, score))

    # ── Plafond qualité sources — protège contre l'inflation de score ──────────
    # Sans aucune source premium (officielle ou grand retailer), la confiance
    # maximale est limitée : on ne peut pas valider un prix sans référence fiable.
    if premium_count == 0:
        # Aucune source premium → plafonner à 82 (= "Fiable", pas "Très fiable")
        score = min(score, 82)
    elif premium_count == 1:
        # 1 source premium → plafonner à 93 (très bon mais pas parfait)
        score = min(score, 93)
    # ≥2 sources premium → pas de plafond supplémentaire (confiance maximale possible)

    # Plafond données limitées : 1 seule source fiable → max 78
    if data_limited and trusted_sources_count <= 1:
        score = min(score, 78)

    score = max(0, min(100, score))

    # ── Labels — alignés avec les seuils frontend JS ───────────────────────────
    if score >= 88:
        label, color = "Très fiable", "green"
    elif score >= 72:
        label, color = "Fiable", "green"
    elif score >= 55:
        label, color = "Acceptable", "orange"
    elif score >= 38:
        label, color = "Prudence", "orange"
    else:
        label, color = "Risque élevé", "red"

    return {"score": score, "label": label, "color": color}


def _build_explanation(
    score: int,
    trusted_count: int,
    excluded_count: int,
    premium_count: int,
    distinct_shops: int,
    data_limited: bool,
) -> str:
    """Génère une explication textuelle humaine du score."""
    if data_limited and trusted_count == 0:
        return "Données insuffisantes pour évaluer la fiabilité de ce prix."

    parts: list[str] = []

    # Facteur positif principal
    if premium_count >= 2:
        parts.append(f"{premium_count} sources premium détectées (boutiques officielles ou grands retailers)")
    elif premium_count == 1:
        parts.append("1 source premium détectée")

    if trusted_count >= 5:
        parts.append(f"{trusted_count} sources fiables utilisées pour ce calcul")
    elif trusted_count >= 2:
        parts.append(f"{trusted_count} sources validées")

    if distinct_shops >= 5:
        parts.append(f"bonne diversité ({distinct_shops} boutiques distinctes)")

    # Facteurs négatifs
    if excluded_count > 0:
        parts.append(
            f"{excluded_count} offre{'s' if excluded_count > 1 else ''} exclue{'s' if excluded_count > 1 else ''} "
            f"(prix trop éloigné de la médiane ou source suspecte)"
        )

    if data_limited:
        parts.append("données limitées — estimation prudente")

    if not parts:
        if score >= 70:
            return "Prix cohérents entre les sources analysées."
        return "Analyse basée sur un nombre limité de sources."

    # Synthèse
    if score >= 85:
        intro = "Score élevé"
    elif score >= 70:
        intro = "Score correct"
    elif score >= 55:
        intro = "Score modéré"
    else:
        intro = "Score faible"

    return f"{intro} — " + " · ".join(parts) + "."


def _neutral_report(brand: str, model: str) -> dict[str, Any]:
    """Rapport neutre retourné en cas de données insuffisantes ou d'erreur."""
    return {
        "trusted_min_price":        None,
        "trusted_avg_price":        None,
        "trusted_median_price":     None,
        "weighted_avg_price":       None,
        "trusted_sources_count":    0,
        "excluded_anomalies_count": 0,
        "total_sources_count":      0,
        "premium_sources_count":    0,
        "distinct_shops_count":     0,
        "premium_shop_names":       [],
        "trust_score":              60,
        "trust_label":              "Données limitées",
        "trust_color":              "orange",
        "explanation":              "Données insuffisantes pour évaluer la fiabilité.",
        "anomaly_details":          [],
        "data_limited":             True,
        "tier_counts":              {},
    }
