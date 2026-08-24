"""SQLite-backed durable session mapping."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, MutableMapping
from pathlib import Path

from .models import SessionRecord, SessionState


class SQLiteSessionStore(MutableMapping[str, SessionRecord]):
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS flyt_sessions (
                    instance_uuid TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    reservation_id TEXT NOT NULL,
                    port_id TEXT,
                    service_ip TEXT,
                    generation INTEGER NOT NULL DEFAULT 1,
                    state TEXT NOT NULL,
                    backend_session_id TEXT,
                    bootstrap_token TEXT,
                    failure_reason TEXT,
                    history_json TEXT NOT NULL
                )
            """)
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(flyt_sessions)")
            }
            for name, definition in (
                ("port_id", "TEXT"), ("service_ip", "TEXT"),
                ("generation", "INTEGER NOT NULL DEFAULT 1"),
            ):
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE flyt_sessions ADD COLUMN {name} {definition}"
                    )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def __getitem__(self, key: str) -> SessionRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM flyt_sessions WHERE instance_uuid = ?", (key,)
            ).fetchone()
        if row is None:
            raise KeyError(key)
        return SessionRecord(
            instance_uuid=row["instance_uuid"],
            project_id=row["project_id"],
            profile=row["profile"],
            reservation_id=row["reservation_id"],
            port_id=row["port_id"],
            service_ip=row["service_ip"],
            generation=row["generation"],
            state=SessionState(row["state"]),
            backend_session_id=row["backend_session_id"],
            bootstrap_token=row["bootstrap_token"],
            failure_reason=row["failure_reason"],
            history=[SessionState(value) for value in json.loads(row["history_json"])],
        )

    def __setitem__(self, key: str, value: SessionRecord) -> None:
        if key != value.instance_uuid:
            raise ValueError("session key must equal instance UUID")
        with self._connect() as connection:
            connection.execute("""
                INSERT INTO flyt_sessions (
                    instance_uuid, project_id, profile, reservation_id,
                    port_id, service_ip, generation, state,
                    backend_session_id, bootstrap_token, failure_reason, history_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(instance_uuid) DO UPDATE SET
                    project_id=excluded.project_id,
                    profile=excluded.profile,
                    reservation_id=excluded.reservation_id,
                    port_id=excluded.port_id,
                    service_ip=excluded.service_ip,
                    generation=excluded.generation,
                    state=excluded.state,
                    backend_session_id=excluded.backend_session_id,
                    bootstrap_token=excluded.bootstrap_token,
                    failure_reason=excluded.failure_reason,
                    history_json=excluded.history_json
            """, (
                value.instance_uuid,
                value.project_id,
                value.profile,
                value.reservation_id,
                value.port_id,
                value.service_ip,
                value.generation,
                value.state.value,
                value.backend_session_id,
                value.bootstrap_token,
                value.failure_reason,
                json.dumps([state.value for state in value.history]),
            ))

    def __delitem__(self, key: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM flyt_sessions WHERE instance_uuid = ?", (key,)
            )
            if cursor.rowcount == 0:
                raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT instance_uuid FROM flyt_sessions ORDER BY instance_uuid"
            ).fetchall()
        return iter(row["instance_uuid"] for row in rows)

    def __len__(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM flyt_sessions").fetchone()[0])
