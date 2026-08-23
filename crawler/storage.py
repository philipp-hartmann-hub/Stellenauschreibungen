"""SQLite persistence for crawled jobs."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from models import Job


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    uid TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    location TEXT,
    posted_at TEXT,
    deadline TEXT,
    source_id TEXT NOT NULL,
    source_name TEXT NOT NULL,
    ebene TEXT NOT NULL,
    land TEXT,
    adapter TEXT NOT NULL,
    raw_json TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_jobs_active ON jobs(active);
CREATE INDEX IF NOT EXISTS idx_jobs_source ON jobs(source_id);
CREATE INDEX IF NOT EXISTS idx_jobs_ebene ON jobs(ebene);
CREATE INDEX IF NOT EXISTS idx_jobs_land ON jobs(land);
CREATE INDEX IF NOT EXISTS idx_jobs_last_seen ON jobs(last_seen);

CREATE TABLE IF NOT EXISTS crawl_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    jobs_seen INTEGER DEFAULT 0,
    sources_ok INTEGER DEFAULT 0,
    sources_failed INTEGER DEFAULT 0,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS source_stats (
    run_id     INTEGER NOT NULL,
    source_id  TEXT NOT NULL,
    adapter    TEXT,
    count      INTEGER NOT NULL,
    status     TEXT NOT NULL,
    error      TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, source_id)
);

CREATE INDEX IF NOT EXISTS idx_source_stats_source ON source_stats(source_id);
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class JobStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def start_run(self) -> int:
        cur = self.conn.execute(
            "INSERT INTO crawl_runs (started_at) VALUES (?)",
            (utcnow_iso(),),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        *,
        jobs_seen: int,
        sources_ok: int,
        sources_failed: int,
        notes: str = "",
    ) -> None:
        self.conn.execute(
            """
            UPDATE crawl_runs
            SET finished_at = ?, jobs_seen = ?, sources_ok = ?, sources_failed = ?, notes = ?
            WHERE id = ?
            """,
            (utcnow_iso(), jobs_seen, sources_ok, sources_failed, notes, run_id),
        )
        self.conn.commit()

    def record_source_stat(
        self,
        run_id: int,
        source_id: str,
        adapter: str | None,
        count: int,
        status: str,
        error: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO source_stats (
                run_id, source_id, adapter, count, status, error, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, source_id) DO UPDATE SET
                adapter = excluded.adapter,
                count = excluded.count,
                status = excluded.status,
                error = excluded.error,
                created_at = excluded.created_at
            """,
            (run_id, source_id, adapter, count, status, error, utcnow_iso()),
        )
        self.conn.commit()

    def list_source_stats_for_run(self, run_id: int) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT run_id, source_id, adapter, count, status, error, created_at
            FROM source_stats
            WHERE run_id = ?
            ORDER BY source_id
            """,
            (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_recent_source_stats(
        self,
        source_id: str,
        *,
        exclude_run_id: int,
        limit: int = 10,
    ) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT ss.run_id, ss.count, ss.status, ss.created_at
            FROM source_stats ss
            INNER JOIN crawl_runs cr ON cr.id = ss.run_id
            WHERE ss.source_id = ?
              AND ss.run_id != ?
              AND cr.finished_at IS NOT NULL
            ORDER BY ss.run_id DESC
            LIMIT ?
            """,
            (source_id, exclude_run_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def upsert_jobs(self, jobs: Sequence[Job], seen_at: str | None = None) -> set[str]:
        """Insert/update jobs; return set of uids touched in this batch."""
        ts = seen_at or utcnow_iso()
        touched: set[str] = set()
        for job in jobs:
            touched.add(job.uid)
            existing = self.conn.execute(
                "SELECT first_seen FROM jobs WHERE uid = ?",
                (job.uid,),
            ).fetchone()
            first_seen = existing["first_seen"] if existing else ts
            self.conn.execute(
                """
                INSERT INTO jobs (
                    uid, title, url, location, posted_at, deadline,
                    source_id, source_name, ebene, land, adapter, raw_json,
                    first_seen, last_seen, active
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(uid) DO UPDATE SET
                    title = excluded.title,
                    url = excluded.url,
                    location = excluded.location,
                    posted_at = excluded.posted_at,
                    deadline = excluded.deadline,
                    source_id = excluded.source_id,
                    source_name = excluded.source_name,
                    ebene = excluded.ebene,
                    land = excluded.land,
                    adapter = excluded.adapter,
                    raw_json = excluded.raw_json,
                    last_seen = excluded.last_seen,
                    active = 1
                """,
                (
                    job.uid,
                    job.title,
                    job.url,
                    job.location,
                    job.posted_at,
                    job.deadline,
                    job.source_id,
                    job.source_name,
                    job.ebene,
                    job.land,
                    job.adapter,
                    json.dumps(job.raw, ensure_ascii=False) if job.raw else None,
                    first_seen,
                    ts,
                ),
            )
        self.conn.commit()
        return touched

    def mark_missing_inactive(
        self,
        source_ids: Iterable[str],
        seen_uids: set[str],
        seen_at: str | None = None,
    ) -> int:
        """Mark jobs for given sources inactive if not seen in this run."""
        ts = seen_at or utcnow_iso()
        source_ids = list(source_ids)
        if not source_ids:
            return 0
        placeholders = ",".join("?" for _ in source_ids)
        rows = self.conn.execute(
            f"SELECT uid FROM jobs WHERE active = 1 AND source_id IN ({placeholders})",
            source_ids,
        ).fetchall()
        deactivated = 0
        for row in rows:
            if row["uid"] not in seen_uids:
                self.conn.execute(
                    "UPDATE jobs SET active = 0, last_seen = ? WHERE uid = ?",
                    (ts, row["uid"]),
                )
                deactivated += 1
        self.conn.commit()
        return deactivated

    def export_json(self, path: Path | str, *, active_only: bool = True) -> int:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        sql = "SELECT * FROM jobs"
        if active_only:
            sql += " WHERE active = 1"
        sql += " ORDER BY last_seen DESC, title COLLATE NOCASE"
        rows = [dict(r) for r in self.conn.execute(sql).fetchall()]
        for row in rows:
            row["active"] = bool(row["active"])
            if row.get("raw_json"):
                try:
                    row["raw"] = json.loads(row["raw_json"])
                except json.JSONDecodeError:
                    row["raw"] = None
            row.pop("raw_json", None)
        path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        return len(rows)

    def list_active(self) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT uid, title, url, location, posted_at, deadline,
                   source_id, source_name, ebene, land, adapter,
                   first_seen, last_seen, active
            FROM jobs WHERE active = 1
            ORDER BY last_seen DESC, title COLLATE NOCASE
            """
        ).fetchall()
        return [dict(r) for r in rows]
