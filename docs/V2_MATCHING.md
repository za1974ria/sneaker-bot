# V2 Matching Strict

## Activer / desactiver

- Ajouter dans `.env`:
  - `ENABLE_STRICT_MATCHING=false`
  - `V2_DRY_RUN=true`
- Tant que `ENABLE_STRICT_MATCHING=false`, seuls les endpoints `v1` sont utilises en prod.
- Les endpoints v2 sont accessibles en parallele via `/api/v2/*`.

## Seed catalogue canonique

- Lancer `python scripts/build_canonical_catalog.py`
- Cette commande alimente `canonical_products` depuis `static/sneakers_db.json`.

## Dry-run v2 (sans ecriture DB)

- Lancer:
  - `python scripts/run_v2_dry_run.py --output /tmp/v2_report.json`
- Le JSON retourne:
  - `raw_count`
  - `rejected_filter`
  - `rejected_match`
  - `rejected_outlier`
  - `final_count`
  - `top_rejections`

## Rollback

- Remettre `ENABLE_STRICT_MATCHING=false`
- Continuer a utiliser uniquement les routes v1.
- Optionnel: vider `matched_rows_v2` si besoin d'un reset du dataset v2.
