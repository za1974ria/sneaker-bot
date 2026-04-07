# Audit SneakerBot — 28 mars 2026

Audit en **lecture seule** (aucune modification du code applicatif). Commandes exécutées sur l’hôte au moment de l’audit.

---

## Infrastructure

| Élément | Résultat |
|--------|----------|
| **systemctl** | `sneaker_bot.service` **active (running)** — uvicorn `app.app:app`, `--host 0.0.0.0 --port 5003` |
| **Port 5003** | **LISTEN** sur `0.0.0.0:5003`, processus `uvicorn` (PID observé au moment de l’audit) |
| **Disque** (`df -h` sur `/`) | ~145 Go total, ~7 % utilisé, espace libre confortable |
| **Données** (`/root/sneaker_bot/data/`) | Répertoire sur la même partition racine (pas de point de montage dédié) |

### Fichiers SQLite (`data/*.db`)

| Fichier | Taille approx. |
|---------|----------------|
| `aegis_verifier.db` | 24 Ko |
| `price_alerts.db` | 16 Ko |
| `price_history.db` | 56 Ko |

### Fichiers CSV principaux (`data/*.csv`)

| Fichier | Taille approx. |
|---------|----------------|
| `market_fr.csv` | 12 Ko |
| `market_fr_sources.csv` | 12 Ko |
| `market_fr_quality_report.csv` | 8 Ko |

**Remarque** : `systemctl` signale que l’unité a changé sur disque — prévoir `systemctl daemon-reload` si le fichier service a été modifié récemment.

---

## Données

### `market_fr.csv`

| Indicateur | Valeur |
|------------|--------|
| Modèles (lignes) | **148** |
| Prix moyen (avg) | **114,83 €** |
| Lignes avec `price_avg == 0` | **0** |
| Colonnes | `brand`, `model`, `market`, `price_min`, `price_max`, `price_avg`, `updated_at`, `nb_sources`, `groq_valid`, `groq_confidence` |
| Dernière `updated_at` (max) | **2026-03-28 18:41:31** |
| `nb_sources` (moyenne) | **~31,8** |
| `groq_valid == True` | **148 / 148** |

### Catalogue `models_list.json`

- **148** entrées.
- **0** modèle catalogue absent de `market_fr.csv` (couverture catalogue ↔ CSV **100 %** sur les clés `brand|model` en minuscules).

### `market_fr_quality_report.csv` (signal interne qualité / doublons)

| Statut | Lignes |
|--------|--------|
| OK | 56 |
| SUSPECT | 44 |

*(Fichier ~100 lignes de données + en-tête.)*

### Bases absentes au chemin attendu par le script d’audit

- **`data/claude_credibility_cache.db`** : **absent** (pas de cache SQLite crédibilité Claude sur disque).

---

## IAs

| IA | Statut | Modèle testé / configuré | Cache / persistance |
|----|--------|---------------------------|---------------------|
| **Groq** | OK | `llama-3.1-8b-instant` | `groq_price_validation_cache.json` — **1** clé racine au moment du test *(fichier présent)* |
| **Claude (Anthropic)** | Échec API | `claude-haiku-4-5-20251001` | Pas de `claude_credibility_cache.db` |
| **AEGIS** | Config actif, peu d’historique | `AEGIS_CLAUDE_MODEL` = `claude-sonnet-4-20250514` | `aegis_verifier.db` — **0** ligne dans `aegis_checks` |

### Détail Claude

- Clé présente et préfixe `sk-ant-` conforme.
- Appel API : **HTTP 400** — solde / crédits insuffisants (`credit balance is too low`).

### Détail AEGIS (`AEGIS_VERIFIER`)

- `AEGIS_VERIFIER` : **activé** (`1` au moment de l’audit).
- `AEGIS_MAX_PER_RUN` : **12**
- `AEGIS_CACHE_TTL_HOURS` : **18**

**Synthèse** : la chaîne **Groq** est opérationnelle pour un test minimal ; **Claude** n’est pas utilisable tant que le compte n’est pas crédité ; **AEGIS** dépend de Claude et n’a **aucune vérification enregistrée** dans la base au moment de l’audit.

---

## Routes API (`http://127.0.0.1:5003`, sans cookie / sans en-tête d’auth)

| Route | HTTP |
|-------|------|
| `GET /health` | **200** |
| `GET /api/sneakers?limit=5` | **403** |
| `GET /api/analytics/stats` | **403** |
| `GET /api/history/stats` | **403** |
| `GET /api/alerts/count` | **403** |
| `GET /api/aegis/recent` | **403** |
| `GET /api/export/woocommerce-csv` | **200** |
| `GET /api/export/prestashop-csv` | **200** |

**Interprétation** : les **403** sur une partie des routes indiquent un **contrôle d’accès** (comportement attendu pour des endpoints non publics). Les exports et le health répondent **200** sans session.

---

## Scheduler

Le script d’audit proposé utilisait `sched = start_scheduler()` puis `sched.get_jobs()` — or **`start_scheduler()` retourne `None`** dans `scheduler.py` (effet de bord uniquement). L’audit statique du code confirme les jobs suivants une fois le scheduler démarré par l’app :

| ID | Rôle |
|----|------|
| `scraper_interval` | Rafraîchissement sources FR — **cron** minute `5` chaque heure |
| `sources_nightly` | Enrichissement nuit — **03:30** |
| `run_market_fr_hourly` | Agrégat marché FR — **toutes les 5 h** (minute `0`) |
| `startup_hourly_refresh_if_stale` | Job ponctuel si données FR jugées obsolètes au boot |

Les journaux **systemd** au démarrage confirment : *« Scraper scheduler started (hourly sources @:05, run_market every 5h @:00, nightly @03:30) »*.

---

## Scrapers (`ACTIVE_SCRAPERS_FR`)

**17** scrapers actifs listés par le module :

Courir, Foot Locker, Snipes, Sports Direct, Spartoo, Zalando, Basket4Ballers, Intersport, Snipes (fr-fr), Sport 2000 (grande surface), WeTheNew, Nike FR, Adidas FR, Klekt, New Balance FR, Asics FR, Puma FR.

---

## Fiabilité des sources et des chiffres (estimation)

Ces indicateurs **ne remplacent pas** une revue métier manuelle ; ils résument l’état **observable** des fichiers et de l’API au moment de l’audit.

| Métrique | Lecture | Taux / commentaire |
|----------|---------|---------------------|
| Couverture catalogue → `market_fr.csv` | 148/148 | **100 %** des paires catalogue présentes dans l’agrégat |
| Validation Groq sur l’agrégat (`groq_valid`) | 148/148 `True` | **100 %** des lignes marquées validées côté CSV |
| Rapport qualité interne (`market_fr_quality_report.csv`) | 56 OK / 44 SUSPECT | **~56 %** des lignes du rapport en **OK**, **~44 %** en **SUSPECT** (règles internes, ex. doublons / anomalies) |
| Profondeur scraping (proxy) | `nb_sources` moyen ~**31,8** | Bonne **diversité de sources** par modèle en moyenne |
| Cohérence prix | Aucun `price_avg` nul | Les moyennes publiées sont **strictement positives** dans l’agrégat |

**Synthèse fiabilité (ordre de grandeur)** :

- **Chiffres agrégés + validation Groq (fichier)** : élevée sur la dimension *« présence et validation booléenne »* (**~100 %** des lignes).
- **Qualité heuristique locale** (`quality_report`) : **modérée** — environ **la moitié** des entrées du rapport sont encore classées **SUSPECT** ; à traiter au cas par cas (causes dans la colonne `reasons`).
- **Disponibilité des IAs externes** : **Groq OK** ; **Claude indisponible** (crédits) — tout flux reposant sur Anthropic (AEGIS, certains caches) est **à risque** tant que la facturation n’est pas rétablie.

---

## Scores globaux (avis d’audit, échelle /10)

| Domaine | Note | Justification courte |
|---------|------|----------------------|
| Infrastructure | **9** | Service stable, port ouvert, disque sain ; petit point sur `daemon-reload`. |
| Données | **8** | Couverture et agrégat propres ; rapport qualité avec part non négligeable de SUSPECT. |
| IAs | **5** | Groq fonctionnel ; Claude bloqué ; AEGIS sans historique en base. |
| API | **7** | Health + exports publics OK ; reste protégé par 403 sans auth (cohérent). |
| Scheduler / scrapers | **8** | Planification claire dans le code + log de démarrage ; 17 scrapers FR déclarés. |

**SCORE TOTAL (moyenne simple)** : **7,4 / 10**

---

## Actions recommandées (par priorité)

1. **Créditer le compte Anthropic** ou ajuster le plan — sinon Claude, AEGIS et tout appel `Anthropic` resteront en échec.
2. **`systemctl daemon-reload`** si le fichier unit `sneaker_bot.service` a été modifié.
3. **Passer en revue les 44 lignes SUSPECT** du `market_fr_quality_report.csv` (colonnes `reasons`) pour prioriser corrections scraping / dédup.
4. **Documenter / automatiser le test scheduler** : utiliser l’API interne ou un import du `_scheduler` réservé aux tests, plutôt que de supposer un retour de `start_scheduler()`.
5. **Pour les tests API** : utiliser les mêmes en-têtes / session que le front ou un client authentifié pour valider les routes actuellement en **403**.

---

*Rapport généré automatiquement à partir des sorties de commandes d’audit du 28 mars 2026.*
