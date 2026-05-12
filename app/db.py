"""SQLite access layer for SneakerBot runtime data."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class DatabaseManager:
    """Simple reusable manager for sneakerbot.db tables."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_tables(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS access_control (
                id INTEGER PRIMARY KEY,
                users TEXT,
                sales_mode TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                id TEXT PRIMARY KEY,
                status TEXT,
                data TEXT,
                updated_at TEXT
            )
            """
        )

    def get_access_control(
        self,
        *,
        default_users: list[dict[str, Any]],
        default_sales_mode: str = "open",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "sales_mode": default_sales_mode if default_sales_mode in {"open", "closed"} else "open",
            "users": list(default_users),
        }
        try:
            with self._connect() as conn:
                self._ensure_tables(conn)
                row = conn.execute(
                    "SELECT users, sales_mode FROM access_control ORDER BY id ASC LIMIT 1"
                ).fetchone()
                if row is None:
                    return payload

                sales_mode = str(row["sales_mode"] or "open").strip().lower()
                if sales_mode in {"open", "closed"}:
                    payload["sales_mode"] = sales_mode

                users_raw = str(row["users"] or "").strip()
                if users_raw:
                    loaded = json.loads(users_raw)
                    if isinstance(loaded, list):
                        payload["users"] = loaded
        except Exception:
            logger.exception("DB get_access_control failed")
        return payload

    def save_access_control(self, payload: dict[str, Any]) -> bool:
        try:
            sales_mode = str(payload.get("sales_mode") or "open").strip().lower()
            if sales_mode not in {"open", "closed"}:
                sales_mode = "open"
            users = payload.get("users")
            if not isinstance(users, list):
                users = []
            users_json = json.dumps(users, ensure_ascii=False)
            with self._connect() as conn:
                self._ensure_tables(conn)
                conn.execute(
                    """
                    INSERT INTO access_control(id, users, sales_mode)
                    VALUES(1, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        users=excluded.users,
                        sales_mode=excluded.sales_mode
                    """,
                    (users_json, sales_mode),
                )
            return True
        except Exception:
            logger.exception("DB save_access_control failed")
            return False

    def list_subscriptions_by_status(self, status: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        try:
            with self._connect() as conn:
                self._ensure_tables(conn)
                rows = conn.execute(
                    """
                    SELECT data FROM subscriptions
                    WHERE status=?
                    ORDER BY updated_at DESC, id DESC
                    """,
                    (status,),
                ).fetchall()
            for row in rows:
                try:
                    obj = json.loads(str(row["data"] or "{}"))
                    if isinstance(obj, dict):
                        out.append(obj)
                except Exception:
                    continue
        except Exception:
            logger.exception("DB list_subscriptions_by_status failed: %s", status)
        return out

    def upsert_subscription(self, sub: dict[str, Any], *, status: str) -> bool:
        try:
            sub_id = str(sub.get("id") or "").strip()
            if not sub_id:
                return False
            payload = dict(sub)
            payload["status"] = status
            updated_at = str(payload.get("validated_at") or payload.get("rejected_at") or payload.get("submitted_at") or "")
            if not updated_at:
                from datetime import datetime, timezone

                updated_at = datetime.now(timezone.utc).isoformat()
            with self._connect() as conn:
                self._ensure_tables(conn)
                conn.execute(
                    """
                    INSERT INTO subscriptions(id, status, data, updated_at)
                    VALUES(?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        status=excluded.status,
                        data=excluded.data,
                        updated_at=excluded.updated_at
                    """,
                    (sub_id, status, json.dumps(payload, ensure_ascii=False), updated_at),
                )
            return True
        except Exception:
            logger.exception("DB upsert_subscription failed")
            return False

