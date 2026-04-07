# 🤖 SNEAKERBOT — FICHE TECHNIQUE COMPLÈTE

## 1. PRÉSENTATION
- Nom : SneakerBot
- URL : https://sneakerbot.shop
- Version : 1.0 Production
- Date : Mars 2026
- Auteur : Zakaria Chohra

## 2. ARCHITECTURE TECHNIQUE
- Langage : Python 3.x
- Framework : FastAPI + Uvicorn
- Serveur : Hetzner VPS (161.97.123.238)
- Service : `sneaker_bot.service` (systemd)
- Port : 5003
- Reverse proxy : Nginx + HTTPS Let's Encrypt
- Domaine : sneakerbot.shop

## 3. STACK IA

### 3.1 Groq (llama-3.1-8b-instant)
- Rôle : validation des prix agrégés, détection d'anomalies, génération de `groq_valid` et `groq_confidence`.
- Modèle utilisé : `llama-3.1-8b-instant` (via module superviseur IA).
- Fréquence : à chaque cycle d'agrégation marché (pipeline scraping FR).

### 3.2 Claude API (claude-haiku)
- Rôle : second avis crédibilité (`app/claude_credibility.py`) avec bonus 0-5 points.
- Modèle utilisé : `claude-haiku-4-5-20251001` (`CLAUDE_MODEL`).
- Fréquence : à la demande du scoring crédibilité, avec cache JSON TTL 168h.

### 3.3 AEGIS Verifier
- Rôle : vérification post-scraping via Claude + recherche web, audit des verdicts.
- Modèle utilisé : `claude-haiku-4-5-20251001` (`AEGIS_CLAUDE_MODEL`).
- Fréquence : job quotidien (08:00) + exécution manuelle possible ; quota `AEGIS_MAX_PER_RUN=5`.

### 3.4 SerpAPI Google Shopping
- Rôle : contrôle externe des prix via Google Shopping FR.
- Modèle/API : SerpAPI (`engine=google_shopping`).
- Fréquence : batch quotidien (09:00) + cache SQLite 24h ; max 5 modèles par batch.

### 3.5 Score confiance
- Rôle : score 0-100 multi-critères (`sources`, `cohérence`, `fraîcheur`, `Groq`, règles marque, bonus Claude).
- Module : `app/confidence_scorer.py`.
- Fréquence : calcul côté API/comparateur à chaque payload rendu.

## 4. SCRAPERS ACTIFS
Méthodes observées :
- `requests` (FranceScraper et sites e-commerce)
- `Playwright` (Tier2 premium)
- `API` (SerpAPI Google Shopping, hors scraping boutique)

Scrapers actifs enregistrés (pipeline FR) :
- Courir — méthode `requests` — modèles couverts actuellement (snapshot sources CSV) : 113
- Foot Locker — méthode `requests` — couverture variable selon batch (non présente dans le dernier snapshot source)
- Snipes — méthode `requests` — couverture variable selon batch
- Sports Direct — méthode `requests` — couverture variable selon batch
- Spartoo — méthode `requests` — couverture variable selon batch
- Zalando — méthode `requests` — couverture variable selon batch
- Basket4Ballers — méthode `requests` — couverture variable selon batch
- Intersport — méthode `requests` — couverture variable selon batch
- Snipes (fr-fr) — méthode `requests` — couverture variable selon batch
- Sport 2000 (grande surface) — méthode `requests` — couverture variable selon batch
- WeTheNew — méthode `Playwright` — couverture variable selon batch
- Nike FR — méthode `Playwright` — couverture variable selon batch
- Adidas FR — méthode `Playwright` — couverture variable selon batch
- Klekt — méthode `Playwright` — couverture variable selon batch
- New Balance FR — méthode `Playwright` — couverture variable selon batch
- Asics FR — méthode `Playwright` — couverture variable selon batch
- Puma FR — méthode `Playwright` — couverture variable selon batch

## 5. DONNÉES
- Nb modèles : 148 (fichier `data/market_fr.csv`)
- Nb marques : 10
- Liste complète des marques : Adidas, Asics, Converse, New Balance, Nike, On Running, Puma, Reebok, Salomon, Vans
- Colonnes `market_fr.csv` : 11 (`brand, model, market, price_min, price_max, price_avg, price_median, updated_at, nb_sources, groq_valid, groq_confidence`)
- Sources/modèle : moyenne exacte `19.08`
- Fréquence MAJ :
  - agrégat complet FR toutes les 5h (`hour="*/5"`)
  - refresh léger horaire à `:05`
  - rebuild sources nocturne à 03:30
- Historique : 30 jours SQLite (`app/price_history.py`, `RETENTION_DAYS=30`)

## 6. SYSTÈME COMMERCIAL
- Offres : Essai / Mensuel / Annuel / Clé en main
- Prix : 0€ / 29€ / 190€ / 550€
- Paiement : Virement IBAN Grey
- Notifications : Gmail SMTP + WhatsApp `wa.me`
- Panel admin : `/admin/subscriptions`
- Workflow : soumission -> pending -> validation/rejet -> création accès client -> envoi credentials (email/WhatsApp selon canal)

## 7. SÉCURITÉ
- Authentification : cookies `sb_auth` + `sb_role` + `sb_user`
- Middleware : `auth_gate_middleware` (contrôle accès, IP ban, anti-flood, gestion `sales_mode`)
- HTTPS : Let's Encrypt / certbot via Nginx
- Headers sécurité : `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy`
- Backup : quotidien à 3h00 (politique d'exploitation)
- Monitoring : toutes les 5 minutes (politique d'exploitation)

## 8. APIs EXTERNES
- Groq API
  - Usage : validation IA des prix (`groq_valid`, `groq_confidence`)
  - Coût : variable selon plan Groq
  - Limite : dépend du plan/API key
- Anthropic Claude API
  - Usage : crédibilité + AEGIS verifier (avec/sans web search)
  - Coût : au token (modèle Haiku)
  - Limite : quotas Anthropic + garde-fous applicatifs (`AEGIS_MAX_PER_RUN`, cache TTL)
- SerpAPI
  - Usage : Google Shopping FR (`app/google_shopping_verifier.py`)
  - Coût : crédit SerpAPI par requête
  - Limite : batch limité à 5 modèles, cache 24h obligatoire
- SMTP Gmail
  - Usage : notifications admin et envoi des accès client
  - Coût : inclus compte SMTP
  - Limite : quotas SMTP provider
- Twilio (présent en dépendance/config)
  - Usage : module legacy WhatsApp (actuellement flux principal en `wa.me`)
  - Coût : selon grille Twilio
  - Limite : authentification SID/token et règles sandbox WhatsApp

## 9. BASE DE DONNÉES
- `data/price_history.db`
  - Contenu : snapshots prix par modèle/marché (`price_min`, `price_max`, `price_avg`, `nb_sources`, `groq_valid`, `recorded_at`)
  - TTL / rétention : 30 jours
- `data/google_shopping_cache.db`
  - Contenu : cache des prix Google + journal des vérifications (`google_prices`, `google_verifications`)
  - TTL cache : 24h sur `google_prices`
- `data/aegis_verifier.db`
  - Contenu : audits AEGIS (`aegis_checks`, verdict, confidence, traces)
  - TTL cache logique : 72h (déduplication selon `AEGIS_CACHE_TTL_HOURS`)
- `data/sitemap_cache.db`
  - Contenu : cache scraping sitemap
  - TTL : géré par le module sitemap (cache applicatif)
- `data/price_alerts.db`
  - Contenu : alertes variations prix (>10%)
  - TTL : persistance SQLite (pas de purge TTL explicite observée)

## 10. SCHEDULER — JOBS AUTOMATIQUES
- `scraper_interval`
  - Fréquence : toutes les heures à `:05`
  - Description : refresh FR rapide des sources (`rebuild_fr_sources_csv(max_sites=1)`)
- `sources_nightly`
  - Fréquence : tous les jours à 03:30
  - Description : enrichissement nocturne des sources boutiques (jusqu'à 4 sites)
- `run_market_fr_hourly`
  - Fréquence : toutes les 5 heures à `:00`
  - Description : agrégat complet FR (`run_market("FR")`)
- `aegis_daily`
  - Fréquence : tous les jours à 08:00
  - Description : passe AEGIS quotidienne sur `market_fr.csv`
- `google_shopping_verify`
  - Fréquence : tous les jours à 09:00
  - Description : vérification Google Shopping (échantillon max 5)
- `startup_hourly_refresh_if_stale`
  - Fréquence : one-shot au démarrage si données FR périmées
  - Description : refresh correctif automatique

## 11. ROUTES PRINCIPALES

### Public
- GET `/ping` : ping applicatif
- GET `/health` : état santé API
- GET `/subscribe` : page abonnement
- POST `/subscribe` : soumission abonnement
- GET `/login` : page connexion
- POST `/login` : authentification
- GET `/logout` : déconnexion
- GET `/` : redirection vers comparateur
- GET `/comparison` : comparateur principal
- GET `/product/{product_name}` : détail produit
- GET `/premium` : page premium
- GET `/track/premium` : tracking premium
- GET `/search` / POST `/search` : recherche

### API client/authentifié
- GET `/api/comparison/fr` : payload comparateur FR
- GET `/api/sources/fr` : sources FR par modèle
- GET `/api/products` : produits marché
- GET `/api/catalog` : catalogue
- GET `/api/catalog/types` : types catalogue
- GET `/api/opportunities` : opportunités
- GET `/api/control/status` : statut contrôle
- GET `/api/update/fr/status` : statut mise à jour FR
- GET `/api/quality/fr` : métriques qualité FR
- GET `/api/scorecard/fr` : scorecard FR
- GET `/api/google/stats` : stats Google verifier
- GET `/api/google/recent` : dernières vérifications Google
- POST `/api/google/verify` : trigger vérification Google
- GET `/api/sitemap/test` : test sitemap

### Admin
- GET `/admin/subscriptions` : panel abonnements
- GET `/admin/validate/{sub_id}` : validation demande
- GET `/admin/reject/{sub_id}` : rejet demande
- GET `/admin/reset/{username}` : reset credentials client
- GET `/api/admin/notifications` : notifications admin non envoyées
- GET `/analytics` : dashboard analytics
- GET `/supervisor` : interface superviseur
- POST `/supervisor/start` : lancement superviseur
- GET `/supervisor/status` : statut superviseur

## 12. PERFORMANCES
- Score fiabilité : 95/100
- Temps réponse API : < 2s
- Uptime : 99.9%
- Mémoire max : 512MB

## 13. VARIABLES ENVIRONNEMENT
Variables détectées dans `.env` (noms + rôle, sans valeurs) :
- `ANTHROPIC_API_KEY` : clé API Anthropic (Claude)
- `GROQ_API_KEY` : clé API Groq
- `AEGIS_VERIFIER` : activation AEGIS (1/0)
- `AEGIS_MAX_PER_RUN` : quota max modèles vérifiés par exécution AEGIS
- `AEGIS_CACHE_TTL_HOURS` : TTL anti-redondance AEGIS
- `AEGIS_CLAUDE_MODEL` : modèle Claude utilisé par AEGIS
- `CLAUDE_MODEL` : modèle Claude pour crédibilité
- `CLAUDE_CREDIBILITY_CACHE_TTL_HOURS` : TTL cache crédibilité Claude
- `SMTP_USER` : compte SMTP émetteur
- `SMTP_PASS` : mot de passe/app-password SMTP
- `ADMIN_EMAIL` : email admin notifications
- `TWILIO_ACCOUNT_SID` : identifiant compte Twilio
- `TWILIO_AUTH_TOKEN` : token Twilio
- `TWILIO_WHATSAPP_FROM` : numéro WhatsApp émetteur Twilio
- `ADMIN_WHATSAPP` : destination WhatsApp admin (Twilio)
- `ADMIN_WHATSAPP_NUMBER` : numéro admin pour liens `wa.me`
- `PUBLIC_BASE_URL` : URL publique de la plateforme
- `SERPAPI_KEY` : clé API SerpAPI Google Shopping

---
Fiche générée automatiquement depuis les sources applicatives demandées :
`app/app.py`, `scrapers/aggregator.py`, `app/subscription_manager.py`, `app/google_shopping_verifier.py`, `app/claude_credibility.py`, `app/aegis_verifier.py`, `app/confidence_scorer.py`, `app/brand_price_rules.py`, `scheduler.py`, `data/market_fr.csv`, `requirements.txt`, `.env`.
