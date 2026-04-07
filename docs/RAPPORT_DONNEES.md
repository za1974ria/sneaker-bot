# Résultats scraping complet — 28 mars 2026

## Résumé exécution

| Indicateur | Valeur |
|------------|--------|
| **Durée totale** | **≈ 752,7 s** (~**12,5 min**) — fin à 13:25:30 (fuseau serveur) |
| **Statut** | Succès — `market=FR scraped/updated ok` |
| **Modèles dans le catalogue** (`data/models_list.json`) | **148** |
| **Lignes dans `data/market_fr.csv` après run** | **147** |
| **Modèles sans ligne agrégée** | **1** — *Salomon ACS Pro Women* (aucune ligne nouvelle + pas de ligne historique conservée selon la logique agrégateur) |

### Sources configurées pour ce run

Pipeline FR : **4 sites cœur** (Courir, Foot Locker, Snipes, Sports Direct) + **6 sources « extra »** enregistrées dans `TIER1_EXTRA_SCRAPER_CLASSES` (Spartoo, Zalando, Basket4Ballers, Intersport, Snipes fr-fr, Sport 2000 grande surface / Playwright).  
Les requêtes partent en parallèle (plusieurs workers) ; chaque modèle interroge jusqu’à la limite `FR_SCRAPER_MAX_SITES` (défaut = toutes ces sources).

### Fichiers de log

- **Extrait terminal** (dernières lignes du run, incl. durée) : copie possible depuis la session Cursor ayant exécuté `run_market('FR')` ; référence interne `terminals/775302.txt`.
- **Copie d’analyse** : `docs/_analyse_data.txt` (sortie texte de l’analyse pandas locale).
- Les chemins `/tmp/scraping_log.txt` et `/tmp/analyse_data.txt` demandés dans la consigne n’ont pas pu être remplis automatiquement depuis cet environnement ; utiliser les fichiers ci-dessus ou relancer la commande avec `tee` sur votre machine.

### Erreurs / messages notables (résumé)

- **Aucune exception** remontée sur la fin du run (`exit_code: 0`).
- Nombreuses lignes **INFO** du type *« Prix manuels utilisés pour … »* : le fichier `data/manual_prices.json` a été appliqué pour de nombreux modèles (souvent variantes *Women* et certaines marques), ce qui **remplace ou complète** les agrégats issus du scraping brut lorsque la logique métier le déclenche.
- Aucune trace d’**ERROR** dans l’extrait de log disponible (fin de run).

---

## Qualité des données

### Fichier analysé

- **Chemin** : `data/market_fr.csv`
- **Colonnes** : `brand`, `model`, `market`, `price_min`, `price_max`, `price_avg`, `updated_at`
- **Lignes** : **147**

### Stats globales

- **Prix moyen global** (moyenne des `price_avg`) : **112,49 €**
- **Prix min global** (`price_min` minimum) : **40,00 €**
- **Prix max global** (`price_max` maximum) : **250,00 €**

### Couverture par marque (nombre de lignes CSV)

| Marque | Modèles |
|--------|--------:|
| Nike | 25 |
| Adidas | 18 |
| New Balance | 17 |
| Puma | 14 |
| Asics | 13 |
| Converse | 13 |
| Reebok | 13 |
| On Running | 12 |
| Vans | 12 |
| Salomon | 10 |

### Couverture catalogue

- **Total catalogue** : 148  
- **Avec ligne dans `market_fr.csv`** : 147 (**99,3 %**)  
- **Sans ligne** : 1 (**0,7 %**)

### Qualité des prix (règles de l’analyse)

- **Prix min &lt; 30 €** : **0** modèle  
- **Prix max &gt; 800 €** : **0** modèle  
- **Écart (max − min) / moyenne &gt; 200 %** : **0** modèle  

### Fraîcheur (`updated_at`)

- **Données &lt; 2 h** : **147** lignes  
- **Données &gt; 24 h** : **0**  
- **Dernière horodatage max observée** sur l’échantillon : **2026-03-28 13:12:57** (pandas ; fuseau selon serveur)

### Répartition des prix moyens

| Tranche | Nb modèles |
|---------|------------|
| &lt; 50 € | 4 |
| 50 – 100 € | 54 |
| 100 – 150 € | 66 |
| 150 – 200 € | 23 |
| 200 – 300 € | 0 |
| &gt; 300 € | 0 |

---

## Modèles problématiques

### Sans aucune ligne agrégée dans `market_fr.csv`

- **Salomon ACS Pro Women** — présent dans `models_list.json`, absent du CSV après ce run (scrape vide + pas de conservation / pas de prix manuel pour cette clé).

### Prix suspects (&lt; 30 € ou &gt; 800 €)

- **Aucun** sur les 147 lignes actuelles.

### Recommandations

- **Catalogue** : décider si *Salomon ACS Pro Women* doit rester ; si oui, ajouter une entrée dans `manual_prices.json` ou améliorer les requêtes scrape pour ce libellé.
- **Prix manuels** : le volume d’application manuelle sur ce run est élevé — à documenter côté métier (sneakers féminins / marques à faible signal scrape).

---

## Sources les plus performantes

- **`data/market_fr_sources.csv`** ne contient après ce run **que l’en-tête** (aucune ligne source). Il n’est donc **pas possible** de classer les boutiques par nombre de prix valides ni de compter les erreurs par source à partir de ce fichier.
- **Interprétation possible** : soit `scrape_model_by_site` n’a retourné aucun prix par boutique pour les clés traitées (tous les `by_site` vides), soit le **filtrage strict** des lignes sources (`_is_valid_triplet`) a éliminé toutes les entrées, soit une combinaison des deux — **investigation code / prochain run** hors périmètre de ce rapport (consigne : pas de modification de code durant cette étape).

---

*Rapport généré automatiquement à partir du run `run_market('FR')` et des fichiers `data/market_fr.csv`, `data/models_list.json`, `data/market_fr_sources.csv`.*
