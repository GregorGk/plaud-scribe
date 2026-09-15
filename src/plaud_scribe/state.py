"""SQLite record of what has been transcribed and uploaded.

Sync is idempotent because of this table: a recording is only processed when it is
absent or previously failed, and Drive file IDs are remembered so a re-upload updates
the existing file instead of creating a second copy.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS recordings (
    id             TEXT PRIMARY KEY,
    name           TEXT,
    recorded_at    TEXT,
    duration_seconds REAL,
    status         TEXT NOT NULL DEFAULT 'pending',
    attempts       INTEGER NOT NULL DEFAULT 0,
    error          TEXT,
    provider       TEXT,
    model_id       TEXT,
    cost_usd       REAL,
    languages      TEXT,
    speaker_count  INTEGER,
    transcribed_at TEXT,
    uploaded_at    TEXT,
    drive_files    TEXT
);
CREATE INDEX IF NOT EXISTS recordings_status ON recordings(status);
"""

STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


@dataclass
class Record:
    id: str
    name: str = ""
    recorded_at: str | None = None
    duration_seconds: float = 0.0
    status: str = STATUS_PENDING
    attempts: int = 0
    error: str | None = None
    provider: str | None = None
    model_id: str | None = None
    cost_usd: float | None = None
    languages: str | None = None
    speaker_count: int | None = None
    transcribed_at: str | None = None
    uploaded_at: str | None = None
    drive_files: str | None = None

    @property
    def drive(self) -> dict[str, str]:
        if not self.drive_files:
            return {}
        try:
            return json.loads(self.drive_files)
        except json.JSONDecodeError:
            return {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        with closing(self.conn.cursor()) as cur:
            cur.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def get(self, recording_id: str) -> Record | None:
        row = self.conn.execute(
            "SELECT * FROM recordings WHERE id = ?", (recording_id,)
        ).fetchone()
        return Record(**dict(row)) if row else None

    def note_seen(self, recording_id: str, name: str, recorded_at: str | None, duration: float) -> Record:
        """Insert a placeholder row without disturbing an existing one."""
        self.conn.execute(
            """
            INSERT INTO recordings (id, name, recorded_at, duration_seconds)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                recorded_at = COALESCE(excluded.recorded_at, recordings.recorded_at),
                duration_seconds = excluded.duration_seconds
            """,
            (recording_id, name, recorded_at, duration),
        )
        self.conn.commit()
        record = self.get(recording_id)
        assert record is not None
        return record

    def mark_done(self, recording_id: str, **fields: Any) -> None:
        drive = fields.pop("drive_files", None)
        self.conn.execute(
            """
            UPDATE recordings SET
                status = ?, error = NULL, attempts = attempts + 1,
                provider = ?, model_id = ?, cost_usd = ?, languages = ?,
                speaker_count = ?, transcribed_at = ?, uploaded_at = ?, drive_files = ?
            WHERE id = ?
            """,
            (
                STATUS_DONE,
                fields.get("provider"),
                fields.get("model_id"),
                fields.get("cost_usd"),
                fields.get("languages"),
                fields.get("speaker_count"),
                fields.get("transcribed_at") or _now(),
                fields.get("uploaded_at"),
                json.dumps(drive) if drive else None,
                recording_id,
            ),
        )
        self.conn.commit()

    def mark_failed(self, recording_id: str, error: str) -> None:
        self.conn.execute(
            """
            UPDATE recordings
               SET status = ?, error = ?, attempts = attempts + 1
             WHERE id = ?
            """,
            (STATUS_FAILED, error[:1000], recording_id),
        )
        self.conn.commit()

    def is_done(self, recording_id: str) -> bool:
        record = self.get(recording_id)
        return record is not None and record.status == STATUS_DONE

    def by_status(self, status: str) -> list[Record]:
        rows = self.conn.execute(
            "SELECT * FROM recordings WHERE status = ? ORDER BY recorded_at DESC", (status,)
        ).fetchall()
        return [Record(**dict(row)) for row in rows]

    def recent(self, limit: int = 20) -> list[Record]:
        rows = self.conn.execute(
            "SELECT * FROM recordings ORDER BY COALESCE(recorded_at, '') DESC LIMIT ?", (limit,)
        ).fetchall()
        return [Record(**dict(row)) for row in rows]

    def spend_since(self, iso_date: str) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM recordings WHERE transcribed_at >= ?",
            (iso_date,),
        ).fetchone()
        return float(row["total"] or 0.0)

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM recordings GROUP BY status"
        ).fetchall()
        return {row["status"]: row["n"] for row in rows}


def languages_field(languages: Iterable[str]) -> str:
    return ",".join(languages)
