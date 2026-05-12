"""
Application Celery partagée par le worker (`celery -A celery_app worker`)
et l'API FastAPI (import pour enregistrer les tâches et broker URL).
"""

import os
import sys

from celery import Celery

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# Garantit PYTHONPATH pour les ForkPoolWorkers.
# Quand une task importe app.scheduler (lazy), le fork hérite de sys.path
# mais PYTHONPATH peut ne pas être propagé selon le mode de démarrage.
pythonpath = os.environ.get("PYTHONPATH", "")
if BASE_DIR not in pythonpath.split(os.pathsep):
    os.environ["PYTHONPATH"] = (
        f"{BASE_DIR}{os.pathsep}{pythonpath}" if pythonpath else BASE_DIR
    )

print("[CELERY] sys.path fixed")

import scrapers  # noqa: F401

print("[CELERY] scrapers import OK")

# Pré-importer app.scheduler dans le process principal pour que les workers
# forkés héritent du module déjà chargé (évite ModuleNotFoundError en fork).
try:
    import app.scheduler as _scheduler_preload  # noqa: F401
    print("[CELERY] app.scheduler pre-imported OK")
except Exception as _e:
    print(f"[CELERY] app.scheduler pre-import warn: {_e}")

redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# include= : enregistre les @task sans import circulaire au chargement de FastAPI
app = Celery(
    "sneakerbot",
    broker=redis_url,
    backend=redis_url,
    include=["app.fr_job_runner"],
)

app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Europe/Paris",
    # Avec brand-by-brand scraping, concurrency=1 (solo, pas de contention) :
    # ~2976s mesuré (10 marques × ~300s/marque). Marge 500s avant soft limit 3500s.
    task_time_limit=4200,
    task_soft_time_limit=3900,
    worker_prefetch_multiplier=1,
)
