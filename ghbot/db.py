"""SQLite persistence: operations, backups, repository locks, settings, badges."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 4

_MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE operations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        repo TEXT,
        stage TEXT NOT NULL,
        params TEXT NOT NULL DEFAULT '{}',
        impact TEXT,
        result TEXT,
        error TEXT,
        backup_id TEXT,
        confirm_phrase TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        expires_at TEXT
    );
    CREATE INDEX idx_operations_stage ON operations(stage);
    CREATE INDEX idx_operations_created ON operations(created_at);

    CREATE TABLE backups (
        id TEXT PRIMARY KEY,
        repo TEXT NOT NULL,
        path TEXT NOT NULL,
        status TEXT NOT NULL,
        reason TEXT,
        operation_id INTEGER,
        ref_count INTEGER,
        commit_count INTEGER,
        size_bytes INTEGER,
        bundle_sha256 TEXT,
        created_at TEXT NOT NULL,
        verified_at TEXT,
        error TEXT
    );
    CREATE INDEX idx_backups_repo ON backups(repo);

    CREATE TABLE backup_counters (day TEXT PRIMARY KEY, last INTEGER NOT NULL);

    CREATE TABLE repo_locks (
        repo_key TEXT PRIMARY KEY,
        operation_id INTEGER,
        holder TEXT NOT NULL,
        acquired_at TEXT NOT NULL
    );

    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

    CREATE TABLE badges (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        label TEXT NOT NULL,
        config TEXT NOT NULL,
        position INTEGER NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE TABLE readme_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        repo TEXT NOT NULL,
        path TEXT NOT NULL,
        sha TEXT,
        content TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """,
    2: """
    ALTER TABLE backups ADD COLUMN visibility TEXT;
    CREATE TABLE favorites (
        repo_key TEXT PRIMARY KEY,
        full_name TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE protected_repos (
        repo_key TEXT PRIMARY KEY,
        full_name TEXT NOT NULL,
        reason TEXT,
        created_at TEXT NOT NULL
    );
    """,
    4: """
    CREATE TABLE pull_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account TEXT NOT NULL,
        repo TEXT NOT NULL,
        number INTEGER,
        branch TEXT NOT NULL,
        base TEXT NOT NULL,
        change_type TEXT NOT NULL,
        title TEXT NOT NULL,
        status TEXT NOT NULL,
        auto_merge INTEGER NOT NULL DEFAULT 0,
        merge_method TEXT NOT NULL DEFAULT 'squash',
        delete_branch INTEGER NOT NULL DEFAULT 1,
        head_sha TEXT,
        html_url TEXT,
        operation_id INTEGER,
        error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        merged_at TEXT
    );
    CREATE INDEX idx_pull_requests_status ON pull_requests(status);
    CREATE INDEX idx_pull_requests_repo ON pull_requests(repo);
    """,
    3: """
    CREATE TABLE auth (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        algorithm TEXT NOT NULL,
        salt BLOB NOT NULL,
        hash BLOB NOT NULL,
        params TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        failed_attempts INTEGER NOT NULL DEFAULT 0,
        locked_until TEXT
    );
    """,
}


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def parse_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass
class Operation:
    id: int
    kind: str
    repo: str | None
    stage: str
    params: dict[str, Any]
    impact: dict[str, Any] | None
    result: dict[str, Any] | None
    error: str | None
    backup_id: str | None
    confirm_phrase: str | None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None

    @property
    def expired(self) -> bool:
        return self.expires_at is not None and self.expires_at < utcnow()


@dataclass
class PullRequestRecord:
    id: int
    account: str
    repo: str
    number: int | None
    branch: str
    base: str
    change_type: str
    title: str
    status: str  # opening | open | merging | merged | closed | failed
    auto_merge: bool
    merge_method: str
    delete_branch: bool
    head_sha: str | None
    html_url: str | None
    operation_id: int | None
    error: str | None
    created_at: datetime
    updated_at: datetime
    merged_at: datetime | None


@dataclass
class BackupRecord:
    id: str
    repo: str
    path: str
    status: str
    reason: str | None
    operation_id: int | None
    ref_count: int | None
    commit_count: int | None
    size_bytes: int | None
    bundle_sha256: str | None
    created_at: datetime
    verified_at: datetime | None
    error: str | None
    visibility: str | None = None


def _row_to_operation(row: sqlite3.Row) -> Operation:
    return Operation(
        id=row["id"],
        kind=row["kind"],
        repo=row["repo"],
        stage=row["stage"],
        params=json.loads(row["params"] or "{}"),
        impact=json.loads(row["impact"]) if row["impact"] else None,
        result=json.loads(row["result"]) if row["result"] else None,
        error=row["error"],
        backup_id=row["backup_id"],
        confirm_phrase=row["confirm_phrase"],
        created_at=parse_iso(row["created_at"]),  # type: ignore[arg-type]
        updated_at=parse_iso(row["updated_at"]),  # type: ignore[arg-type]
        expires_at=parse_iso(row["expires_at"]),
    )


def _row_to_pull_request(row: sqlite3.Row) -> PullRequestRecord:
    data = dict(row)
    data["auto_merge"] = bool(data["auto_merge"])
    data["delete_branch"] = bool(data["delete_branch"])
    for key in ("created_at", "updated_at", "merged_at"):
        data[key] = parse_iso(data[key])
    return PullRequestRecord(**data)


def _row_to_backup(row: sqlite3.Row) -> BackupRecord:
    data = dict(row)
    data["created_at"] = parse_iso(data["created_at"])
    data["verified_at"] = parse_iso(data["verified_at"])
    return BackupRecord(**data)


class RepoLockedError(Exception):
    def __init__(self, repo: str, holder: str) -> None:
        super().__init__(f"{repo} is locked by another operation ({holder}).")
        self.repo = repo
        self.holder = holder


class Database:
    """Thread-safe SQLite wrapper. Blocking work runs in a worker thread."""

    def __init__(self, path: Path | str) -> None:
        self._path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._migrate()

    # ------------------------------------------------------------------ core
    def _migrate(self) -> None:
        with self._lock:
            self._conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
            row = self._conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
            current = row["v"] or 0
            for version in range(current + 1, SCHEMA_VERSION + 1):
                self._conn.execute("BEGIN")
                try:
                    for statement in _MIGRATIONS[version].split(";"):
                        if statement.strip():
                            self._conn.execute(statement)
                    self._conn.execute("INSERT INTO schema_version(version) VALUES (?)", (version,))
                    self._conn.execute("COMMIT")
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _run(self, fn, *args):
        with self._lock:
            return fn(*args)

    async def _call(self, fn, *args):
        return await asyncio.to_thread(self._run, fn, *args)

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)

    # ------------------------------------------------------------ operations
    async def create_operation(
        self,
        kind: str,
        repo: str | None,
        stage: str,
        *,
        params: dict[str, Any] | None = None,
        impact: dict[str, Any] | None = None,
        confirm_phrase: str | None = None,
        ttl: timedelta | None = None,
    ) -> Operation:
        def op() -> Operation:
            now = utcnow()
            cur = self._execute(
                "INSERT INTO operations(kind, repo, stage, params, impact, confirm_phrase,"
                " created_at, updated_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    kind,
                    repo,
                    stage,
                    json.dumps(params or {}),
                    json.dumps(impact) if impact is not None else None,
                    confirm_phrase,
                    iso(now),
                    iso(now),
                    iso(now + ttl) if ttl else None,
                ),
            )
            row = self._execute("SELECT * FROM operations WHERE id=?", (cur.lastrowid,)).fetchone()
            return _row_to_operation(row)

        return await self._call(op)

    async def get_operation(self, op_id: int) -> Operation | None:
        def op() -> Operation | None:
            row = self._execute("SELECT * FROM operations WHERE id=?", (op_id,)).fetchone()
            return _row_to_operation(row) if row else None

        return await self._call(op)

    async def transition(
        self,
        op_id: int,
        from_stages: Sequence[str],
        to_stage: str,
        **fields: Any,
    ) -> bool:
        """Atomic compare-and-set stage change. Returns False if the stage did not match."""
        allowed = {"impact", "result", "error", "backup_id", "confirm_phrase", "expires_at", "params"}
        sets = ["stage=?", "updated_at=?"]
        values: list[Any] = [to_stage, iso(utcnow())]
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"Unknown operation field {key}")
            if key in {"impact", "result", "params"} and value is not None:
                value = json.dumps(value)
            elif key == "expires_at":
                value = iso(value)
            sets.append(f"{key}=?")
            values.append(value)
        placeholders = ",".join("?" for _ in from_stages)

        def op() -> bool:
            cur = self._execute(
                f"UPDATE operations SET {', '.join(sets)} WHERE id=? AND stage IN ({placeholders})",
                (*values, op_id, *from_stages),
            )
            return cur.rowcount == 1

        return await self._call(op)

    async def update_operation(self, op_id: int, **fields: Any) -> None:
        op = await self.get_operation(op_id)
        if op is not None:
            await self.transition(op_id, [op.stage], op.stage, **fields)

    async def list_operations(
        self, *, limit: int, offset: int = 0, stages: Sequence[str] | None = None
    ) -> tuple[list[Operation], int]:
        clause, params = "", ()
        if stages:
            clause = f"WHERE stage IN ({','.join('?' for _ in stages)})"
            params = tuple(stages)

        def op():
            rows = self._execute(
                f"SELECT * FROM operations {clause} ORDER BY id DESC LIMIT ? OFFSET ?", (*params, limit, offset)
            ).fetchall()
            total = self._execute(f"SELECT COUNT(*) AS c FROM operations {clause}", params).fetchone()["c"]
            return [_row_to_operation(r) for r in rows], total

        return await self._call(op)

    async def operations_in_stages(self, stages: Sequence[str]) -> list[Operation]:
        placeholders = ",".join("?" for _ in stages)

        def op():
            rows = self._execute(
                f"SELECT * FROM operations WHERE stage IN ({placeholders}) ORDER BY id", tuple(stages)
            ).fetchall()
            return [_row_to_operation(r) for r in rows]

        return await self._call(op)

    async def find_awaiting_confirmation(self, phrase: str, stage: str) -> Operation | None:
        def op():
            row = self._execute(
                "SELECT * FROM operations WHERE stage=? AND confirm_phrase=? ORDER BY id DESC LIMIT 1",
                (stage, phrase),
            ).fetchone()
            return _row_to_operation(row) if row else None

        return await self._call(op)

    async def last_operation_with_backup(self, kinds: Sequence[str], stages: Sequence[str]) -> Operation | None:
        k = ",".join("?" for _ in kinds)
        s = ",".join("?" for _ in stages)

        def op():
            row = self._execute(
                f"SELECT * FROM operations WHERE kind IN ({k}) AND stage IN ({s})"
                " AND backup_id IS NOT NULL ORDER BY id DESC LIMIT 1",
                (*kinds, *stages),
            ).fetchone()
            return _row_to_operation(row) if row else None

        return await self._call(op)

    async def log_simple(
        self, kind: str, repo: str | None, stage: str, *, params=None, result=None, error=None
    ) -> Operation:
        op = await self.create_operation(kind, repo, stage, params=params)
        if result is not None or error is not None:
            await self.transition(op.id, [stage], stage, result=result, error=error)
        return op

    # --------------------------------------------------------------- backups
    async def next_backup_id(self, now: datetime | None = None) -> str:
        day = (now or utcnow()).strftime("%Y%m%d")

        def op() -> str:
            row = self._execute(
                "INSERT INTO backup_counters(day, last) VALUES (?, 1)"
                " ON CONFLICT(day) DO UPDATE SET last = last + 1 RETURNING last",
                (day,),
            ).fetchone()
            return f"BK-{day}-{row['last']:03d}"

        return await self._call(op)

    async def insert_backup(self, backup_id: str, repo: str, path: str, reason: str, operation_id: int | None) -> None:
        await self._call(
            self._execute,
            "INSERT INTO backups(id, repo, path, status, reason, operation_id, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (backup_id, repo, path, "creating", reason, operation_id, iso(utcnow())),
        )

    async def update_backup(self, backup_id: str, **fields: Any) -> None:
        allowed = {"status", "ref_count", "commit_count", "size_bytes", "bundle_sha256", "verified_at", "error", "visibility"}
        if not fields:
            return
        for key in fields:
            if key not in allowed:
                raise ValueError(f"Unknown backup field {key}")
        values = [iso(v) if isinstance(v, datetime) else v for v in fields.values()]
        sets = ", ".join(f"{k}=?" for k in fields)
        await self._call(self._execute, f"UPDATE backups SET {sets} WHERE id=?", (*values, backup_id))

    async def get_backup(self, backup_id: str) -> BackupRecord | None:
        def op():
            row = self._execute("SELECT * FROM backups WHERE id=?", (backup_id,)).fetchone()
            return _row_to_backup(row) if row else None

        return await self._call(op)

    async def list_backups(
        self, *, limit: int, offset: int = 0, repo: str | None = None, include_deleted: bool = False,
        public_only: bool = False, status: str | None = None,
    ) -> tuple[list[BackupRecord], int]:
        where, params = [], []
        if public_only:
            where.append("visibility = 'public'")
        if status:
            where.append("status = ?")
            params.append(status)
        if repo:
            where.append("lower(repo)=lower(?)")
            params.append(repo)
        if not include_deleted:
            where.append("status != 'deleted'")
        clause = f"WHERE {' AND '.join(where)}" if where else ""

        def op():
            rows = self._execute(
                f"SELECT * FROM backups {clause} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
            total = self._execute(f"SELECT COUNT(*) AS c FROM backups {clause}", tuple(params)).fetchone()["c"]
            return [_row_to_backup(r) for r in rows], total

        return await self._call(op)

    async def latest_verified_backup(self, repo: str | None = None, public_only: bool = False) -> BackupRecord | None:
        backups, _ = await self.list_backups(limit=1, repo=repo, status="verified", public_only=public_only)
        return backups[0] if backups else None

    # ----------------------------------------------------------------- locks
    async def acquire_lock(self, repo_key: str, holder: str, operation_id: int | None = None) -> None:
        def op() -> None:
            try:
                self._execute(
                    "INSERT INTO repo_locks(repo_key, operation_id, holder, acquired_at) VALUES (?,?,?,?)",
                    (repo_key.lower(), operation_id, holder, iso(utcnow())),
                )
            except sqlite3.IntegrityError:
                row = self._execute("SELECT holder FROM repo_locks WHERE repo_key=?", (repo_key.lower(),)).fetchone()
                raise RepoLockedError(repo_key, row["holder"] if row else "unknown") from None

        await self._call(op)

    async def release_lock(self, repo_key: str, operation_id: int | None = None) -> None:
        if operation_id is None:
            await self._call(self._execute, "DELETE FROM repo_locks WHERE repo_key=?", (repo_key.lower(),))
        else:
            await self._call(
                self._execute,
                "DELETE FROM repo_locks WHERE repo_key=? AND operation_id=?",
                (repo_key.lower(), operation_id),
            )

    async def list_locks(self) -> list[dict[str, Any]]:
        def op():
            return [dict(r) for r in self._execute("SELECT * FROM repo_locks").fetchall()]

        return await self._call(op)

    async def release_all_locks(self) -> int:
        def op():
            return self._execute("DELETE FROM repo_locks").rowcount

        return await self._call(op)

    # -------------------------------------------------------------- settings
    async def get_setting(self, key: str, default: Any = None) -> Any:
        def op():
            row = self._execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return json.loads(row["value"]) if row else default

        return await self._call(op)

    async def set_setting(self, key: str, value: Any) -> None:
        await self._call(
            self._execute,
            "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    # ---------------------------------------------------------------- badges
    async def list_badges(self) -> list[dict[str, Any]]:
        def op():
            rows = self._execute("SELECT * FROM badges ORDER BY position, id").fetchall()
            return [{**dict(r), "config": json.loads(r["config"])} for r in rows]

        return await self._call(op)

    async def add_badge(self, kind: str, label: str, config: dict[str, Any]) -> int:
        def op():
            pos = self._execute("SELECT COALESCE(MAX(position), 0) + 1 AS p FROM badges").fetchone()["p"]
            cur = self._execute(
                "INSERT INTO badges(kind, label, config, position, created_at) VALUES (?,?,?,?,?)",
                (kind, label, json.dumps(config), pos, iso(utcnow())),
            )
            return cur.lastrowid

        return await self._call(op)

    async def delete_badge(self, badge_id: int) -> bool:
        def op():
            return self._execute("DELETE FROM badges WHERE id=?", (badge_id,)).rowcount == 1

        return await self._call(op)

    async def move_badge(self, badge_id: int, delta: int) -> None:
        def op():
            rows = self._execute("SELECT id FROM badges ORDER BY position, id").fetchall()
            ids = [r["id"] for r in rows]
            if badge_id not in ids:
                return
            i = ids.index(badge_id)
            j = max(0, min(len(ids) - 1, i + delta))
            ids.insert(j, ids.pop(i))
            for pos, bid in enumerate(ids, start=1):
                self._execute("UPDATE badges SET position=? WHERE id=?", (pos, bid))

        await self._call(op)

    async def save_readme_snapshot(self, repo: str, path: str, sha: str | None, content: str) -> int:
        def op():
            cur = self._execute(
                "INSERT INTO readme_snapshots(repo, path, sha, content, created_at) VALUES (?,?,?,?,?)",
                (repo, path, sha, content, iso(utcnow())),
            )
            return cur.lastrowid

        return await self._call(op)

    async def latest_readme_snapshot(self, repo: str) -> dict[str, Any] | None:
        def op():
            row = self._execute(
                "SELECT * FROM readme_snapshots WHERE repo=? ORDER BY id DESC LIMIT 1", (repo,)
            ).fetchone()
            return dict(row) if row else None

        return await self._call(op)

    # ------------------------------------------------- favorites / protection
    async def add_favorite(self, full_name: str) -> bool:
        def op():
            cur = self._execute(
                "INSERT OR IGNORE INTO favorites(repo_key, full_name, created_at) VALUES (?,?,?)",
                (full_name.lower(), full_name, iso(utcnow())),
            )
            return cur.rowcount == 1

        return await self._call(op)

    async def remove_favorite(self, full_name: str) -> bool:
        def op():
            return self._execute("DELETE FROM favorites WHERE repo_key=?", (full_name.lower(),)).rowcount == 1

        return await self._call(op)

    async def list_favorites(self) -> list[str]:
        def op():
            return [r["full_name"] for r in self._execute("SELECT full_name FROM favorites ORDER BY full_name").fetchall()]

        return await self._call(op)

    async def protect_repo(self, full_name: str, reason: str | None) -> bool:
        def op():
            cur = self._execute(
                "INSERT OR IGNORE INTO protected_repos(repo_key, full_name, reason, created_at) VALUES (?,?,?,?)",
                (full_name.lower(), full_name, reason, iso(utcnow())),
            )
            return cur.rowcount == 1

        return await self._call(op)

    async def unprotect_repo(self, full_name: str) -> bool:
        def op():
            return self._execute("DELETE FROM protected_repos WHERE repo_key=?", (full_name.lower(),)).rowcount == 1

        return await self._call(op)

    async def get_protection(self, full_name: str) -> dict[str, Any] | None:
        def op():
            row = self._execute("SELECT * FROM protected_repos WHERE repo_key=?", (full_name.lower(),)).fetchone()
            return dict(row) if row else None

        return await self._call(op)

    async def list_protected(self) -> list[dict[str, Any]]:
        def op():
            return [dict(r) for r in self._execute("SELECT * FROM protected_repos ORDER BY full_name").fetchall()]

        return await self._call(op)

    async def quick_check(self) -> str:
        def op():
            return self._execute("PRAGMA quick_check").fetchone()[0]

        return await self._call(op)

    # ------------------------------------------------------------------- auth
    async def get_auth(self) -> dict[str, Any] | None:
        def op():
            row = self._execute("SELECT * FROM auth WHERE id=1").fetchone()
            return dict(row) if row else None

        return await self._call(op)

    async def set_auth(self, algorithm: str, salt: bytes, digest: bytes, params: dict[str, Any]) -> None:
        await self._call(
            self._execute,
            "INSERT INTO auth(id, algorithm, salt, hash, params, updated_at, failed_attempts, locked_until)"
            " VALUES (1,?,?,?,?,?,0,NULL) ON CONFLICT(id) DO UPDATE SET algorithm=excluded.algorithm,"
            " salt=excluded.salt, hash=excluded.hash, params=excluded.params, updated_at=excluded.updated_at,"
            " failed_attempts=0, locked_until=NULL",
            (algorithm, salt, digest, json.dumps(params), iso(utcnow())),
        )

    async def set_auth_failures(self, failed_attempts: int, locked_until: datetime | None) -> None:
        await self._call(
            self._execute,
            "UPDATE auth SET failed_attempts=?, locked_until=? WHERE id=1",
            (failed_attempts, iso(locked_until)),
        )

    # ------------------------------------------------------------ pull requests
    async def create_pull_request(self, **fields: Any) -> PullRequestRecord:
        columns = ("account", "repo", "number", "branch", "base", "change_type", "title", "status",
                   "auto_merge", "merge_method", "delete_branch", "head_sha", "html_url", "operation_id")
        values = [fields.get(c) for c in columns]

        def op() -> PullRequestRecord:
            now = iso(utcnow())
            cur = self._execute(
                f"INSERT INTO pull_requests({', '.join(columns)}, created_at, updated_at)"
                f" VALUES ({', '.join('?' for _ in columns)}, ?, ?)",
                (*values, now, now),
            )
            row = self._execute("SELECT * FROM pull_requests WHERE id=?", (cur.lastrowid,)).fetchone()
            return _row_to_pull_request(row)

        return await self._call(op)

    async def update_pull_request(self, pr_id: int, **fields: Any) -> None:
        allowed = {"number", "status", "head_sha", "html_url", "error", "merged_at", "auto_merge", "operation_id"}
        for key in fields:
            if key not in allowed:
                raise ValueError(f"Unknown pull request field {key}")
        values = [iso(v) if isinstance(v, datetime) else (int(v) if isinstance(v, bool) else v)
                  for v in fields.values()]
        sets = ", ".join(f"{k}=?" for k in fields)
        await self._call(self._execute,
                         f"UPDATE pull_requests SET {sets}, updated_at=? WHERE id=?",
                         (*values, iso(utcnow()), pr_id))

    async def get_pull_request(self, pr_id: int) -> PullRequestRecord | None:
        def op():
            row = self._execute("SELECT * FROM pull_requests WHERE id=?", (pr_id,)).fetchone()
            return _row_to_pull_request(row) if row else None

        return await self._call(op)

    async def list_pull_requests(self, *, limit: int = 20, offset: int = 0, account: str | None = None,
                                 statuses: Sequence[str] | None = None,
                                 repo: str | None = None) -> tuple[list[PullRequestRecord], int]:
        where, params = [], []
        if account:
            where.append("lower(account)=lower(?)")
            params.append(account)
        if repo:
            where.append("lower(repo)=lower(?)")
            params.append(repo)
        if statuses:
            where.append(f"status IN ({','.join('?' for _ in statuses)})")
            params.extend(statuses)
        clause = f"WHERE {' AND '.join(where)}" if where else ""

        def op():
            rows = self._execute(
                f"SELECT * FROM pull_requests {clause} ORDER BY id DESC LIMIT ? OFFSET ?", (*params, limit, offset)
            ).fetchall()
            total = self._execute(f"SELECT COUNT(*) AS c FROM pull_requests {clause}", tuple(params)).fetchone()["c"]
            return [_row_to_pull_request(r) for r in rows], total

        return await self._call(op)

    async def count_active_pull_requests(self, account: str) -> int:
        def op():
            return self._execute(
                "SELECT COUNT(*) AS c FROM pull_requests WHERE lower(account)=lower(?)"
                " AND status IN ('opening','open','merging')", (account,)
            ).fetchone()["c"]

        return await self._call(op)
