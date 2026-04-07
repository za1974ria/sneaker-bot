# Rapport d’évaluation — SneakerBot

**Périmètre analysé :** répertoire `/root/sneaker_bot` (hors `venv/`, hors `__pycache__`).  
**Date du rapport :** 28 mars 2026.  
**Méthode :** lecture du code source et des fichiers de configuration réels ; aucune supposition non étayée.

---

## 1. Vue d’ensemble

### 1.1 Description générale

SneakerBot est une application orientée **veille prix sneakers** (marché **France** pour le comparateur agrégé), avec une **API et une interface web FastAPI** (`app/app.py`), un **scheduler APScheduler** (`scheduler.py`), des **scrapers** basés sur **requests + BeautifulSoup** (`scrapers/scraper_fr.py`, `scrapers/base_scraper.py`), un module optionnel **Playwright** (`scrapers/scraper_playwright_fr.py`), un **superviseur de cohérence des prix** (`app/ai_supervisor.py`), des **services métier** (`app/services/market_service.py`), et des pipelines parallèles plus « catalogue / arbitrage » (`run_pipeline.py`, `run_scraper.py`, CSV type `market_live.csv`).

Le déploiement observé sur l’hôte utilise **systemd** + **uvicorn** sur le module `app.app:app` (voir §2.6).

### 1.2 Stack technique

| Couche | Technologies |
|--------|----------------|
| Runtime | Python 3.12 (venv projet) |
| API / UI | **FastAPI**, **Uvicorn**, réponses HTML inline (peu de templates Jinja dédiés) |
| Données | Fichiers **CSV** (`data/market_fr.csv`, `data/market_fr_sources.csv`, `market_live.csv`, etc.) et **JSON** (`data/models_list.json`, `tracking.json`, `access_control.json`, …) — **pas de SQLite/PostgreSQL dans le cœur métier observé** |
| Scraping | **requests**, **urllib3** (retry), **BeautifulSoup4** ; **Playwright** listé et implémenté à part |
| Planification | **APScheduler** (`BackgroundScheduler`, triggers `cron`) |
| Analyse prix | Règles déterministes dans `AISupervisor` ; dépendance **openai** (SDK) dans `requirements.txt` |
| Autres deps | **pandas**, **python-dotenv**, **Flask** (présent dans `requirements.txt` et code legacy `app/routes/*`, **non utilisé** par l’app FastAPI principale) |

### 1.3 Architecture globale (schéma textuel)

```
[Navigateur / Client]
        │
        ▼
[Nginx / reverse proxy] (hors repo, constat déploiement antérieur)
        │
        ▼
[Uvicorn → FastAPI app.app:app]
        │
        ├── Middleware auth / anti-abus / mode commercial (access_control.json)
        ├── Routes HTML : /comparison, /login, /analytics, /supervisor, /search, …
        ├── Routes API : /api/comparison/fr, /api/sources/fr, /api/scorecard/fr, …
        └── Lifespan → start_scheduler() (scheduler.py)

[Scheduler APScheduler]
        ├── Job horaire : rebuild_fr_sources_csv(max_sites=1)  → data/market_fr_sources.csv
        └── Job nocturne : rebuild_fr_sources_csv(max_sites=4) → idem

[Scraping FR agrégé]
FranceScraper (requests+BS4) → prix par site → aggregator (stats, outliers, CSV)

[Pipelines parallèles]
run_pipeline.py → collectors/orchestrator → market_live.csv
run_scraper.py → scraper_ecommerce → market_live.csv (cron exemple)
```

**Point d’attention architectural :** plusieurs fils de données coexistent (`market_fr*.csv` vs `market_live.csv` / `trader_prices.csv`) ; le front et les APIs FR s’appuient surtout sur les CSV **FR**, pas sur le seul `market_live.csv`.

### 1.4 Endpoints API disponibles (FastAPI principal)

Déclarés dans `app/app.py` (liste non exhaustive des chemins métier) :

| Méthode | Chemin | Rôle (résumé) |
|---------|--------|----------------|
| GET | `/ping`, `/health` | Santé / connectivité sans auth |
| GET/POST | `/login` | Authentification cookie |
| GET | `/logout` | Déconnexion |
| GET | `/api/products`, `/api/catalog`, `/api/catalog/types` | Données catalogue / produits |
| GET | `/api/opportunities` | Opportunités |
| GET | `/api/control/status` | Statut contrôle IA (clés configurées, mode, SDK) |
| GET | `/api/update/fr/status` | État refresh FR (`fr_update_status.json`) |
| GET | `/api/sources/fr` | Sources par boutique (CSV FR) |
| GET | `/api/comparison/fr` | Comparateur agrégé FR |
| GET | `/api/quality/fr`, `/api/scorecard/fr` | Qualité / scorecard |
| POST | `/supervisor/start`, GET `/supervisor/status` | Job superviseur (analyse locale des lignes CSV) |

Les routes `/docs` et `/redoc` sont autorisées par le middleware en développement/exposition selon config.

---

## 2. Fonctionnalités implémentées

### 2.1 Liste fonctionnelle (exhaustive au regard du code)

- **Comparateur FR** avec agrégation **min / max / moy** et filtrage « validé » côté API/UI.
- **Sources par enseigne** (fichier `market_fr_sources.csv`) avec **alias publics** type « Retailer A/B/… » (`_public_shop_alias` dans `app/app.py`).
- **Authentification** par cookie (`sb_auth`, `sb_role`), mode commercial **open/closed** via `data/access_control.json`.
- **Rate limiting / bannissement IP** (fichier `security_state.json`, seuils dans `app/app.py`).
- **Scheduler** : refresh horaire des **sources** + enrichissement nocturne (voir §2.4).
- **Prix manuels** de secours (`manual_prices.json`) et **normalisation / validation** stricte des triplets min/max/avg (`scrapers/aggregator.py`).
- **Superviseur « IA »** : parcours du CSV marché et **scores OK/WARN/ALERT** (`app/ai_supervisor.py`).
- **Analytics** : compteurs locaux JSON (`app/analytics/tracker.py`) — visites recherche, clics premium, conversion affichée sur `/analytics`.
- **Recherche / pages produit** (routes HTML dédiées).
- **Pipeline collectors** (tiers core/extension/long_tail) exportant `market_live.csv` (`run_pipeline.py`).
- **Cron exemple** pour `run_scraper.py` → `market_live.csv` (`crontab_scraper.example`).

### 2.2 Scraping — sites, couverture, méthodes

**Sites actifs codés en dur dans le scraper FR** (ordre logique des sources) :

```44:49:scrapers/scraper_fr.py
    ACTIVE_SOURCES: tuple[tuple[str, str], ...] = (
        ("Courir", "courir.com"),
        ("Foot Locker", "footlocker.fr"),
        ("Snipes", "snipes.com/fr"),
        ("Sports Direct", "sportsdirect.com"),
    )
```

- **Méthode principale (chemin production FR)** : **HTTP + BeautifulSoup** (`FranceScraper` hérite de `BaseScraper`, pas Playwright dans `run_market` / `rebuild_fr_sources_csv`).
- **Playwright** : classe `PlaywrightFranceScraper` dans `scrapers/scraper_playwright_fr.py` avec User-Agent iPhone — **fichier séparé** ; l’agrégateur commente explicitement l’absence de Playwright sur le chemin FR standard :

```129:133:scrapers/aggregator.py
def _scrape_fr_prices(scraper: FranceScraper, brand: str, model: str) -> list[float]:
    """
    FR : uniquement requests (FranceScraper). Pas de Playwright ici.
    Si [] → aucun fallback automatique.
    """
```

**Nombre de modèles suivis :** le fichier `data/models_list.json` contient **148 entrées** `{ "brand", "model" }` (comptage réel par script, pas « ~60 » — l’hypothèse ~60 est **inférieure** à l’état actuel des données).

**Plafond d’extraction par modèle/site :** `MAX_PRICES = 60` dans `FranceScraper`.

### 2.3 Scheduler APScheduler — fréquence et jobs

Dans `start_scheduler()` :

- **Toutes les heures, minute 5** : `_run_fr_hourly_refresh` → `rebuild_fr_sources_csv(max_sites=1, workers=6)`.
- **Chaque nuit 03:30** : `_run_fr_sources_nightly` → `rebuild_fr_sources_csv(max_sites=4)`.
- **Au démarrage** : si données FR considérées périmées (`_is_fr_data_stale`), un job **date** unique relance le refresh horaire après ~20 s.

```205:223:scheduler.py
        _scheduler.add_job(
            _run_fr_hourly_refresh,
            trigger="cron",
            minute=5,
            id="scraper_interval",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        # Job enrichissement sources boutiques la nuit (03:30 heure serveur).
        _scheduler.add_job(
            _run_fr_sources_nightly,
            trigger="cron",
            hour=3,
            minute=30,
            id="sources_nightly",
```

**Constat critique :** `run_market("FR")` existe et met à jour **`market_fr.csv` + `market_fr_sources.csv`**, et est importé dans `scheduler.py`, mais **`start_scheduler()` n’enregistre aucun job qui appelle `run_market`** — seulement `rebuild_fr_sources_csv`. Or `rebuild_fr_sources_csv` **réécrit surtout `market_fr_sources.csv`** et lit `market_fr.csv` en **fallback** sans réécrire systématiquement l’agrégat principal. Conséquence : **risque de désynchronisation / stagnation de `market_fr.csv`** si personne ne lance `run_market` manuellement ou via un autre mécanisme.

### 2.4 « Validation IA » Groq (llama-3.1-8b-instant)

**Dans le code réel, aucun appel HTTP au fournisseur Groq ni aucune référence au modèle `llama-3.1-8b-instant`.**  
La classe `AISupervisor` :

- lit `GROQ_API_KEY` / `GROK_API_KEY` uniquement pour **savoir si une clé est configurée** (`grok_key_configured`) ;
- expose `allow_remote_ai` mais **ne l’utilise nulle part** pour déclencher un appel distant ;
- **`analyze_prices`** applique un **jeu de règles locales** (planchers/plafonds par marque, ratio min/max, cohérence de la moyenne) et renvoie `decision_source: "local_ruleset_v2"`.

Extrait :

```79:112:app/ai_supervisor.py
            floor, ceiling = PRICE_RANGES.get(brand, PRICE_RANGES["default"])
            anomalies: list[str] = []

            if price_min < floor * 0.3:
                anomalies.append(f"Prix minimum suspect: {price_min}€ (plancher: {floor}€)")

            if price_max > ceiling * 2.0:
                anomalies.append(f"Prix maximum suspect: {price_max}€ (plafond: {ceiling}€)")

            # Ratio strict anti-dérive (plus agressif que l'ancien seuil 8x).
            if price_min > 0 and (price_max / price_min) > 2.2:
                anomalies.append(f"Écart min/max suspect: {price_min}€ → {price_max}€")
            # L'avg doit rester cohérent.
            if not (price_min <= price_avg <= price_max):
                anomalies.append(f"Moyenne hors plage: avg={price_avg}€")
            ...
            return {
                ...
                "decision_source": "local_ruleset_v2",
                "control_mode": self.control_mode,
            }
```

**Conclusion :** la « validation IA » annoncée comme Groq dans le cahier des charges utilisateur **n’est pas implémentée** ; il s’agit d’un **contrôle déterministe**, utile mais différent.

### 2.5 Dashboard analytics (UI)

Page `/analytics` : fond **très sombre** (`#0b0b0b`), carte avec bordure, **chiffres en vert** (`#22c55e`), libellés **Visites**, **Clics premium**, **Conversion %** — alimentés par `get_stats()` (JSON local).

```1812:1820:app/app.py
  <style>
    body {{
      margin: 0;
      min-height: 100vh;
      background: #0b0b0b;
      color: #f8fafc;
      font-family: ui-sans-serif, system-ui, Arial, sans-serif;
```

### 2.6 Service systemd

Fichier observé sur l’hôte : `/etc/systemd/system/sneaker_bot.service` :

- **WorkingDirectory** : `/root/sneaker_bot`
- **ExecStart** : `uvicorn app.app:app --host 0.0.0.0 --port 5003 --proxy-headers`
- **Restart** : `always`
- **Variables d’environnement** : clés API (**GEMINI**, **GROQ**) injectées **en clair** dans le fichier unit — **fuite de secret majeure** si le fichier est lisible ou versionné.

### 2.7 Camouflage User-Agent iPhone

Défini dans la classe de base :

```25:37:scrapers/base_scraper.py
    # Camouflage iPhone user-agent
    USER_AGENT = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
        "Mobile/15E148 Safari/604.1"
    )

    DEFAULT_HEADERS = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "fr-FR",
        "Referer": "https://www.google.fr/",
```

Playwright réutilise une chaîne équivalente (`IPHONE_UA` dans `scraper_playwright_fr.py`).  
**Jitter** entre requêtes : `MIN_DELAY_SEC = 2.5`, `MAX_DELAY_SEC = 5.5` (`_sleep_jitter`).

---

## 3. Évaluation technique — forces

1. **Agrégation et prudence statistique** : trimming, cluster autour de la médiane, plages par marque, validation `_is_valid_triplet`, recalage de secours — limite l’affichage de prix aberrants (`scrapers/aggregator.py`).
2. **Session HTTP robuste** : retries urllib3, timeouts, liste de statuts retry (`BaseScraper.__init__`).
3. **Séparation partielle des responsabilités** : scrapers / agrégateur / service marché / routes FastAPI.
4. **Observabilité légère** : `fr_update_status.json`, logs rotate scheduler, fichier `supervisor.log`.
5. **Garde-fous auth** : middleware centralisé, endpoints sensibles protégés, endpoints publics explicitement listés.
6. **Documentation implicite dans le code** : catalogues JSON des sources FR (`fr_ecommerce_sources.json`) avec statuts active/monitoring/disabled.

---

## 4. Évaluation technique — faiblesses et bugs identifiés

1. **Scheduler incomplet par rapport à `run_market`** : pas de job planifié pour `run_market("FR")` alors que la fonction existe et alimente `market_fr.csv` — **incohérence métier majeure** (voir §2.3).
2. **« IA Groq » absente** : clés lues mais pas d’appel modèle ; `ALLOW_REMOTE_AI=1` ne change pas le chemin d’analyse — **écart documentation / produit**.
3. **Sécurité — secrets** : clés API en clair dans **systemd** ; mots de passe utilisateurs en **clair** dans `access_control.json` ; token d’auth par défaut potentiellement faible via variables d’environnement (`AUTH_TOKEN` dans le code).
4. **Tests** : `tests/test_smoke.py` se contente de `assert True` — **aucune couverture réelle**.
5. **Dette / duplication** : deux applications FastAPI (`/root/sneaker_bot/app.py` et `app/app.py`) ; routes **Flask** orphelines (`app/routes/api.py`, `web.py`) ; `main.py` et `templates/index.html` **vides**.
6. **Scraping fragile** : parsing HTML dépendant des boutiques ; risque élevé de **cassage** si DOM ou anti-bot change ; pas de file d’attente ni de circuit breaker par domaine au-delà des retries.
7. **Performance** : parallélisme massif (jusqu’à 16 workers FR) × jitter 2,5–5,5 s peut **charger** les sites et **provoquer des blocages** ; pas de cache HTTP partagé entre workers (une session par scraper/thread).
8. **Cohérence multi-CSV** : `market_service` / `trader_prices` / `market_live` vs pipeline FR — risque de **confusion** sur la « vérité » affichée selon la page.
9. **Fichier parasite** : `=1.0.1` à la racine du projet (probable erreur de commande shell) — bruit dans le dépôt.

---

## 5. Évaluation business — adéquation e-commerce

| Critère | Verdict |
|---------|---------|
| Prêt pour une boutique en production | **Partiellement** : utile en **outil interne** ou **SAS privé** ; pas prêt comme **source de prix publique garantie** sans revue légale (CGU sites, robots.txt, revente de données) et sans durcissement technique. |
| Pertinence min/max/avg | **Bonne intention** ; la médiane/trim/caps rendent l’agrégat **plus crédible** qu’une moyenne brute, mais reste **dépendant** de la qualité du scraping. |
| Couverture modèles | **148 modèles** dans `models_list.json` — **large** pour un POC, **insuffisant** pour un catalogue e-commerce complet ; surtout **sneakers généralistes**, pas tout le stock d’une boutique. |
| Fiabilité affichage public | **Moyenne** : sans validation humaine ou API officielle partenaire, afficher ces prix comme « officiels » est **risqué** (erreurs, délais, promotions). |
| Fréquence de scraping | Refresh **horaire des sources** (1 site) + **nuit** (4 sites) — **raisonnable** pour limiter la charge ; mais l’**agrégat `market_fr.csv`** peut **vieillir** si `run_market` n’est pas lancé — voir critique scheduler. |

---

## 6. Ajustements recommandés (priorisés)

### CRITIQUE

- **Planifier ou fusionner** : soit ajouter un job `run_market("FR")` (avec limite de durée et `max_instances=1`), soit faire en sorte que le job horaire **mette à jour `market_fr.csv`** de façon fiable (un seul chemin clair).
- **Retirer les secrets du fichier systemd** : utiliser `EnvironmentFile=-/etc/sneaker_bot/secrets.env` avec permissions root-only, ou gestionnaire de secrets.
- **Hacher les mots de passe** dans `access_control.json` (bcrypt/argon2) et **ne jamais** commiter ce fichier avec des credentials réels.

### IMPORTANT

- **Implémenter ou retirer** la mention Groq : soit appel API réel avec repli sur `local_ruleset_v2`, soit renommer l’UI en « contrôle heuristique ».
- **Tests d’intégration minimaux** : import app, `/ping`, lecture CSV, un test `FranceScraper` mock HTTP.
- **Nettoyage** : supprimer ou documenter `app.py` racine vs `app/app.py` ; retirer Flask des requirements si inutilisé.
- **Monitoring** : alertes si `fr_update_status` indique échec répété ou si CSV non mis à jour depuis X heures.

### OPTIONNEL

- File d’attente (Redis/RQ) pour scrapers, cache des pages, historique des prix en série temporelle.
- API d’export stable versionnée pour un thème WooCommerce/Shopify.
- Interface admin CRUD sur `models_list.json`.

---

## 7. Proposition d’évolution vers e-commerce « pro »

| Évolution | Description | Effort indicatif |
|-----------|-------------|------------------|
| **API export prix** | Endpoints REST stables + auth clé API + format JSON aligné SKU boutique | 5–10 j |
| **Cache Redis** | Cache des réponses scraper / pages HTML / rate limit distribué | 3–7 j |
| **PostgreSQL** | Modèles Produit, Prix, Source, Historique ; migration depuis CSV | 10–20 j |
| **Connecteurs CMS** | Plugins ou scripts WooCommerce / Shopify / Presta (webhook, sync SKU) | 15–40 j selon périmètre |
| **Admin modèles** | UI pour activer/désactiver modèles, seuils marque, overrides manuels | 5–12 j |
| **Conformité** | Reprise légale (ToS sites, robots.txt, DSA si applicable) | hors dev pur |

---

## 8. Score global (sur 10)

| Dimension | Note | Commentaire court |
|-----------|------|-------------------|
| **Code quality** | **6,0** | Logique métier présente mais dette (doublons, legacy, fichiers vides). |
| **Robustesse** | **5,0** | Scraping + scheduler incomplet pour l’agrégat principal. |
| **Couverture données** | **6,5** | 148 modèles FR + 4 enseignes actives ; pas tout le marché. |
| **Prêt production** | **4,0** | Secrets en clair, tests fictifs, risques légaux scraping non adressés dans le code. |
| **Valeur business** | **6,0** | Bon socle veille / comparateur interne ; pas « prix garanti » sans travail supplémentaire. |
| **SCORE TOTAL** (moyenne) | **5,5 / 10** | Projet **utilisable avec garde-fous** ; pas encore **produit e-commerce public** sans les correctifs critiques. |

---

## 9. Conclusion

SneakerBot est un **projet ambitieux et déjà riche en fonctionnalités** (comparateur FR, sources multi-boutiques, garde-fous statistiques, auth, scheduler, superviseur de cohérence). En revanche, le **rapport entre intention « IA Groq » et implémentation réelle est nul** : tout repose sur des **règles locales**. La **planification actuelle ne lance pas `run_market`**, ce qui peut laisser **`market_fr.csv` obsolète** alors que les sources sont rafraîchies — **priorité absolue #1** : aligner scheduler et fichiers de vérité (un seul flux ou job `run_market` planifié). Côté exploitation, les **secrets en clair** (systemd + JSON utilisateurs) sont un **blocage production**. Une fois ces points corrigés et la conformité scraping clarifiée, le socle a un **potentiel commercial** surtout en **B2B / outil interne** ou **comparateur privé**, moins comme **flux de prix grand public sans curation**.

---

**Fichier généré :** `docs/RAPPORT_EVALUATION_SNEAKERBOT.md` (ce document).  
**Confirmation :** le fichier a bien été créé sous `/root/sneaker_bot/docs/RAPPORT_EVALUATION_SNEAKERBOT.md`.
