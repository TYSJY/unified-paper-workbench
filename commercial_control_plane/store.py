"""SQLite repository for the local development control plane."""

from __future__ import annotations

import json
import hmac
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = 1


class ControlPlaneStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=15.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 15000")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connection() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    version INTEGER NOT NULL
                );
                INSERT INTO schema_meta(version)
                SELECT 1
                WHERE NOT EXISTS (SELECT 1 FROM schema_meta);

                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS login_codes (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id),
                    salt TEXT NOT NULL,
                    code_hash TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_login_codes_user
                    ON login_codes(user_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id),
                    token_hash TEXT NOT NULL UNIQUE,
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS subscriptions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id),
                    plan TEXT NOT NULL,
                    status TEXT NOT NULL,
                    starts_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_subscriptions_user
                    ON subscriptions(user_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id),
                    device_id TEXT NOT NULL,
                    installation_id TEXT NOT NULL,
                    app_version TEXT NOT NULL,
                    os_family TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    revoked_at TEXT NOT NULL DEFAULT '',
                    UNIQUE(user_id, device_id)
                );

                CREATE TABLE IF NOT EXISTS licence_audit (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id),
                    device_id TEXT NOT NULL,
                    plan TEXT NOT NULL,
                    envelope_json TEXT NOT NULL,
                    issued_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS releases (
                    id TEXT PRIMARY KEY,
                    product_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    envelope_json TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    UNIQUE(product_id, version, channel)
                );

                CREATE TABLE IF NOT EXISTS diagnostics (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id),
                    event_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            version = db.execute("SELECT version FROM schema_meta").fetchone()
            if version is None or int(version["version"]) != SCHEMA_VERSION:
                raise RuntimeError("Unsupported control-plane database schema.")

    def get_or_create_user(self, *, user_id: str, email: str, now: str) -> dict[str, Any]:
        with self.connection() as db:
            row = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO users(id, email, created_at) VALUES (?, ?, ?)",
                    (user_id, email, now),
                )
                row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return dict(row)

    def get_user(self, *, user_id: str) -> dict[str, Any] | None:
        with self.connection() as db:
            row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return dict(row) if row is not None else None

    def save_login_code(
        self,
        *,
        code_id: str,
        user_id: str,
        salt: str,
        code_hash: str,
        expires_at: str,
        now: str,
    ) -> None:
        with self.connection() as db:
            db.execute(
                """
                INSERT INTO login_codes(
                    id, user_id, salt, code_hash, expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (code_id, user_id, salt, code_hash, expires_at, now),
            )

    def consume_login_code(
        self,
        *,
        user_id: str,
        code_hash_resolver: Any,
        now: str,
        maximum_attempts: int,
    ) -> bool:
        """Atomically consume the newest valid code for one user."""

        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """
                SELECT * FROM login_codes
                WHERE user_id = ? AND consumed_at = '' AND expires_at >= ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (user_id, now),
            ).fetchone()
            if row is None or int(row["attempts"]) >= maximum_attempts:
                return False
            expected_hash = code_hash_resolver(str(row["salt"]))
            if not hmac.compare_digest(expected_hash, str(row["code_hash"])):
                db.execute(
                    "UPDATE login_codes SET attempts = attempts + 1 WHERE id = ?",
                    (row["id"],),
                )
                return False
            db.execute(
                "UPDATE login_codes SET consumed_at = ? WHERE id = ? AND consumed_at = ''",
                (now, row["id"]),
            )
            return db.total_changes == 1

    def save_session(
        self,
        *,
        session_id: str,
        user_id: str,
        token_hash: str,
        expires_at: str,
        now: str,
    ) -> None:
        with self.connection() as db:
            db.execute(
                """
                INSERT INTO sessions(id, user_id, token_hash, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, user_id, token_hash, expires_at, now),
            )

    def resolve_session(self, *, token_hash: str, now: str) -> dict[str, Any] | None:
        with self.connection() as db:
            row = db.execute(
                """
                SELECT sessions.*, users.email
                FROM sessions JOIN users ON users.id = sessions.user_id
                WHERE token_hash = ? AND revoked_at = '' AND expires_at >= ?
                """,
                (token_hash, now),
            ).fetchone()
            return dict(row) if row is not None else None

    def revoke_session(self, *, token_hash: str, now: str) -> bool:
        with self.connection() as db:
            cursor = db.execute(
                """
                UPDATE sessions SET revoked_at = ?
                WHERE token_hash = ? AND revoked_at = ''
                """,
                (now, token_hash),
            )
            return cursor.rowcount == 1

    def latest_subscription(self, *, user_id: str, now: str) -> dict[str, Any] | None:
        with self.connection() as db:
            row = db.execute(
                """
                SELECT * FROM subscriptions
                WHERE user_id = ? AND status = 'active' AND expires_at >= ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (user_id, now),
            ).fetchone()
            return dict(row) if row is not None else None

    def latest_subscription_record(self, *, user_id: str) -> dict[str, Any] | None:
        with self.connection() as db:
            row = db.execute(
                """
                SELECT * FROM subscriptions
                WHERE user_id = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    def save_subscription(
        self,
        *,
        subscription_id: str,
        user_id: str,
        plan: str,
        starts_at: str,
        expires_at: str,
        now: str,
    ) -> None:
        with self.connection() as db:
            db.execute(
                """
                INSERT INTO subscriptions(
                    id, user_id, plan, status, starts_at, expires_at, created_at
                ) VALUES (?, ?, ?, 'active', ?, ?, ?)
                """,
                (subscription_id, user_id, plan, starts_at, expires_at, now),
            )

    def replace_active_subscription(
        self,
        *,
        subscription_id: str,
        user_id: str,
        plan: str,
        starts_at: str,
        expires_at: str,
        now: str,
    ) -> None:
        with self.connection() as db:
            db.execute(
                """
                UPDATE subscriptions SET status = 'replaced'
                WHERE user_id = ? AND status = 'active'
                """,
                (user_id,),
            )
            db.execute(
                """
                INSERT INTO subscriptions(
                    id, user_id, plan, status, starts_at, expires_at, created_at
                ) VALUES (?, ?, ?, 'active', ?, ?, ?)
                """,
                (subscription_id, user_id, plan, starts_at, expires_at, now),
            )

    def active_devices(self, *, user_id: str) -> list[dict[str, Any]]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM devices WHERE user_id = ? AND revoked_at = ''",
                (user_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def devices(self, *, user_id: str) -> list[dict[str, Any]]:
        with self.connection() as db:
            rows = db.execute(
                """
                SELECT id, device_id, installation_id, app_version, os_family,
                       created_at, last_seen_at, revoked_at
                FROM devices
                WHERE user_id = ?
                ORDER BY last_seen_at DESC
                """,
                (user_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def upsert_device(
        self,
        *,
        record_id: str,
        user_id: str,
        device_id: str,
        installation_id: str,
        app_version: str,
        os_family: str,
        now: str,
    ) -> None:
        with self.connection() as db:
            db.execute(
                """
                INSERT INTO devices(
                    id, user_id, device_id, installation_id, app_version,
                    os_family, created_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, device_id) DO UPDATE SET
                    installation_id = excluded.installation_id,
                    app_version = excluded.app_version,
                    os_family = excluded.os_family,
                    last_seen_at = excluded.last_seen_at,
                    revoked_at = ''
                """,
                (
                    record_id,
                    user_id,
                    device_id,
                    installation_id,
                    app_version,
                    os_family,
                    now,
                    now,
                ),
            )

    def get_device(self, *, user_id: str, device_id: str) -> dict[str, Any] | None:
        with self.connection() as db:
            row = db.execute(
                """
                SELECT * FROM devices
                WHERE user_id = ? AND device_id = ? AND revoked_at = ''
                """,
                (user_id, device_id),
            ).fetchone()
            return dict(row) if row is not None else None

    def revoke_device(self, *, user_id: str, device_id: str, now: str) -> bool:
        with self.connection() as db:
            cursor = db.execute(
                """
                UPDATE devices SET revoked_at = ?
                WHERE user_id = ? AND device_id = ? AND revoked_at = ''
                """,
                (now, user_id, device_id),
            )
            return cursor.rowcount == 1

    def save_licence(
        self,
        *,
        licence_id: str,
        user_id: str,
        device_id: str,
        plan: str,
        envelope: dict[str, Any],
        now: str,
    ) -> None:
        with self.connection() as db:
            db.execute(
                """
                INSERT INTO licence_audit(
                    id, user_id, device_id, plan, envelope_json, issued_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    licence_id,
                    user_id,
                    device_id,
                    plan,
                    json.dumps(envelope, ensure_ascii=False, separators=(",", ":")),
                    now,
                ),
            )

    def save_release(
        self,
        *,
        release_id: str,
        product_id: str,
        version: str,
        channel: str,
        envelope: dict[str, Any],
        published_at: str,
    ) -> None:
        with self.connection() as db:
            db.execute(
                """
                INSERT INTO releases(
                    id, product_id, version, channel, envelope_json, published_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(product_id, version, channel) DO UPDATE SET
                    envelope_json = excluded.envelope_json,
                    published_at = excluded.published_at
                """,
                (
                    release_id,
                    product_id,
                    version,
                    channel,
                    json.dumps(envelope, ensure_ascii=False, separators=(",", ":")),
                    published_at,
                ),
            )

    def latest_release(self, *, product_id: str, channel: str) -> dict[str, Any] | None:
        with self.connection() as db:
            rows = db.execute(
                """
                SELECT * FROM releases
                WHERE product_id = ? AND channel = ?
                ORDER BY published_at DESC
                """,
                (product_id, channel),
            ).fetchall()
            if not rows:
                return None
            record = dict(rows[0])
            record["envelope"] = json.loads(record.pop("envelope_json"))
            return record

    def save_diagnostic(
        self,
        *,
        diagnostic_id: str,
        user_id: str,
        event: dict[str, Any],
        now: str,
    ) -> None:
        with self.connection() as db:
            db.execute(
                """
                INSERT INTO diagnostics(id, user_id, event_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    diagnostic_id,
                    user_id,
                    json.dumps(event, ensure_ascii=False, separators=(",", ":")),
                    now,
                ),
            )
