"""Simple local JSON tracker for product analytics."""

from __future__ import annotations

import json
from pathlib import Path

TRACKING_PATH = Path(__file__).resolve().parents[2] / "data" / "tracking.json"
DEFAULT_TRACKING = {
    "premium_clicks": 0,
    "search_views": 0,
}


def _ensure_file() -> None:
    try:
        TRACKING_PATH.parent.mkdir(parents=True, exist_ok=True)
        if not TRACKING_PATH.is_file():
            TRACKING_PATH.write_text(
                json.dumps(DEFAULT_TRACKING, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    except Exception:
        # Never break the app because of analytics writes.
        return


def _load_tracking() -> dict[str, int]:
    _ensure_file()
    try:
        raw = json.loads(TRACKING_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return dict(DEFAULT_TRACKING)
        out = dict(DEFAULT_TRACKING)
        for k in DEFAULT_TRACKING:
            try:
                out[k] = int(raw.get(k, 0))
            except (TypeError, ValueError):
                out[k] = 0
        return out
    except Exception:
        return dict(DEFAULT_TRACKING)


def _save_tracking(data: dict[str, int]) -> None:
    try:
        TRACKING_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        # Never break the app because of analytics writes.
        return


def log_event(event_name: str) -> None:
    """
    Increment a tracked event counter in local JSON.
    """
    data = _load_tracking()
    if event_name not in data:
        data[event_name] = 0
    try:
        data[event_name] = int(data[event_name]) + 1
    except Exception:
        data[event_name] = 1
    _save_tracking(data)


def get_stats() -> dict[str, int]:
    """Read current tracking counters."""
    return _load_tracking()

