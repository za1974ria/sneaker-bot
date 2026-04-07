# Statut des sources de prix (marché FR)

Dernière mise à jour : diagnostic HTTP + corrections Tier 1 (URLs, fallback listing, filtre marque sur les prix bruts).

- **Pipeline actif** : `FranceScraper` (4 sites) + `TIER1_EXTRA_SCRAPER_CLASSES` dans `scrapers/tier1_sites.py` (**13** entrées : 5 e-com + **Sport 2000** + **7 Tier 2** via `scrapers/tier2_sites.py`).
- **Variable** : `FR_SCRAPER_MAX_SITES` limite le nombre de sources par modèle (défaut = toutes les sources enregistrées).

## Classification des problèmes (diagnostic)

| Catégorie | Signification |
|-----------|----------------|
| **CAS A** | HTTP 200 mais 0 prix avec extracteur strict → DOM listing non compatible avec `is_relevant_product` |
| **CAS B** | HTTP 403 / WAF → `requests` insuffisant (Playwright à prévoir) |
| **CAS C** | URL de recherche incorrecte (404, mauvaise route) |
| **CAS D** | SSL / certificat (Chausport + CDN) |

## Corrections appliquées (code)

1. **`_tier1_listing_price_fallback`** : si la page contient la marque (et des tokens du modèle), extraction des prix **sans** filtre d’arbre DOM, puis `filter_prices(brand, …)` comme le reste du bot.
2. **Après extraction stricte / meta** : passage des prix candidats dans **`filter_prices`** ; si tout est hors plage marque → enchaînement sur le fallback listing.
3. **403 / 401** : ignore la réponse si la **marque** n’apparaît pas dans le HTML (shell WAF sans résultats).
4. **URLs corrigées** :
   - **Spartoo** : `https://www.spartoo.com/mobile/search.php?search=` (**CAS C** sur l’ancienne URL).
   - **Basket4Ballers** : `/fr/recherche?search_query=` (**CAS C**).
   - **Intersport** : `/sn-IS/?text=` (**CAS C**).
5. **Chausport** : `verify=False` + log d’avertissement — insuffisant ici (Akamai / certificat) → **retiré du registre actif**.

## Tableau des sites

| Site | Tier | Méthode | Status pipeline | Cause / correction | Test Nike Air Force 1 |
|------|------|---------|-----------------|--------------------|------------------------|
| courir.com | 1 | requests | ✅ actif | (inchangé) | OK (FranceScraper) |
| footlocker.fr | 1 | requests | ✅ actif | (inchangé) | OK |
| snipes.com/fr | 1 | requests | ✅ actif | (inchangé) | OK |
| sportsdirect.com | 1 | requests | ✅ actif | (inchangé) | OK |
| spartoo.com | 1 | requests | ✅ actif | **CAS C+A** — URL mobile + fallback listing | ≥ 1 prix |
| zalando.fr | 1 | requests | ✅ actif | **CAS A** — fallback listing | ≥ 1 prix |
| basket4ballers.com | 1 | requests | ✅ actif | **CAS C** — `/fr/recherche` + filtre marque | ≥ 1 prix |
| intersport.fr | 1 | requests | ✅ actif | **CAS C+A** — `sn-IS/?text=` + fallback | ≥ 1 prix |
| snipes.com/fr-fr | 1 | requests | ✅ actif | (inchangé ; timeouts sporadiques) | ≥ 1 prix |
| jdsports.fr | 1 | requests | ⏸️ désactivé | **CAS B** — HTTP 403 | — |
| chausport.com | 1 | requests | ⏸️ désactivé | **CAS D** — SSL + CDN | — |
| sarenza.com | 1 | requests | ⏸️ désactivé | **CAS B** — HTTP 403, pas de listing | — |
| go-sport.com | 1 | requests | ⏸️ désactivé | **CAS C** — redirection / 402 | — |
| sport2000.fr | 1 | playwright* | ⏸️ désactivé | **CAS A** — SPA, pas de prix en HTML statique | — |
| decathlon.fr | 1 | requests | ⏸️ désactivé | **CAS B** — HTTP 403 | — |
| kickz.com/fr | 1 | requests | ⏸️ désactivé | **CAS B** — HTTP 403 | — |
| footshop.fr | 1 | requests | ⏸️ désactivé | **CAS B** — HTTP 403 | — |
| wethenew.com | 2 | requests + PW | ✅ voir § Tier 2 | intégré `WeThenewScraper` | ≥ 1 prix (AF1) |
| stockx.com | 3 | playwright | désactivé | — | — |

\* *Méthode indiquée pour réactivation future ; scraper non branché tant que le registre est désactivé.*

Les classes **JD, Chausport, Sarenza, Go Sport, Sport 2000, Decathlon, Kickz, Footshop** restent dans `scrapers/tier1_sites.py` mais **ne sont plus** dans `TIER1_EXTRA_SCRAPER_CLASSES` pour éviter les pauses réseau inutiles sur chaque modèle.

---

## Grande distribution & surfaces sport (`scrapers/hypermarches.py`)

Probe HTTP (script utilisateur, UA iPhone OS 16, URLs `nike+air+force+1`, mars 2026) puis tests scraper `Nike` / `Air Force 1`.

**Pipeline** : seules les entrées de `HYPERMARCHE_SCRAPER_CLASSES_REGISTERED` sont fusionnées dans `tier1_sites.TIER1_EXTRA_SCRAPER_CLASSES`. Extraction SPA partagée : `tier1_sites.extract_search_result_prices(html, …)`.

**Garde-fou** : `_scrape_search_urls` ignore **HTTP 404** ; Playwright ne lève pas vers le pipeline (retour `[]`).

| Site | Catégorie (`sources.json`) | HTTP probe | Sélecteurs | Résultats test | Statut |
|------|---------------------------|------------|------------|----------------|--------|
| decathlon.fr | hypermarche / sport | 403 | — | 0 | ⚠️ WAF |
| intersport.fr | hypermarche / sport | 404* | — | — | ✅ prix via **tier1** `IntersportScraper` (`/sn-IS/`) |
| go-sport.com | hypermarche / sport | 402 | — | 0 | ⚠️ redirection |
| sport2000.fr | hypermarche / sport | 200 | heuristique FranceScraper + fallback listing sur HTML Playwright | ≥ 8 prix | ✅ actif (`Sport2000GrandeSurfaceScraper`) |
| auchan.fr | hypermarche | 200 | — (Playwright testé : 0 prix) | 0 | ⏸️ hors pipeline |
| carrefour.fr | hypermarche | 403 | — | 0 | ⚠️ WAF |
| e.leclerc | hypermarche | 200 | — | 0 | ⏸️ SSR vide |
| magasins-u.com | hypermarche | 403 | — | 0 | ⚠️ WAF |
| casino.fr | hypermarche | 200 | — | 0 | ⏸️ redirect accueil |
| monoprix.fr | hypermarche | 403 | — | 0 | ⚠️ WAF |
| cora.fr | hypermarche | 403 | — | 0 | ⚠️ redirect |
| intermarche.com | hypermarche | 404** | — | 0 | ⏸️ |

\* URL probe utilisateur `/recherche/?q=` → 404 ; le scraper actif utilise une autre route.  
\** Variantes testées → souvent 403.

Configuration : `config/sources.json` → **`hypermarches`** (`category`: `hypermarche`, `subcategory` pour sport vs hyper).

---

## Tier 2 premium (`scrapers/tier2_sites.py`)

Enregistrement : `TIER2_SCRAPER_CLASSES_REGISTERED` fusionné dans `tier1_sites.TIER1_EXTRA_SCRAPER_CLASSES` et `aggregator.ALL_EXTRA_SCRAPERS_FR`.

User-Agent : `BaseScraper.USER_AGENT` (iPhone / Safari). Délais 1,5–3 s. Aucune exception propagée (retour `[]`).

| Site | Type | Méthode | Status | Prix test (probe / run) |
|------|------|---------|--------|-------------------------|
| wethenew.com | Secondaire FR | requests → Playwright | ✅ | ~90–300 € (AF1, extract heuristique) |
| nike.com/fr | Officiel | requests → Playwright | ✅ | ~100–130 € (AF1) |
| adidas.fr | Officiel | API JSON PLP | ✅ | ~120–130 € (Samba OG) |
| klekt.com | Secondaire EU | Playwright | ✅ | ≥ 1 prix (Dunk Low, HTML rendu) |
| newbalance.fr | Officiel | requests → Playwright | ⚠️ | 0 (WAF / shell court mars 2026) |
| asics.com/fr | Officiel | requests → Playwright | ⚠️ | 0 (WAF / shell court) |
| puma.com/fr | Officiel | Playwright | ✅ | ~45–150 € (Suede, SPA) |

`config/sources.json` → clé **`tier2`** : `active: true/false` selon disponibilité (NB / Asics désactivés tant que le contournement WAF n’est pas fiable).
