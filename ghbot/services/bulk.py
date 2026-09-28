"""Bulk backup work: backup-all, automatic backups, verify-all, backup diff, storage stats."""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from ghbot.db import RepoLockedError, parse_iso, utcnow
from ghbot.git.mirror import dir_size, important, ls_remote
from ghbot.github.client import GitHubError
from ghbot.logging_setup import get_redactor
from ghbot.services.backups import BackupError
from ghbot.services.policy import PolicyError
from ghbot.services.safety import RUNNING, WAITING, Progress
from ghbot.validators import RepoRef

if TYPE_CHECKING:
    from ghbot.services.container import Services

log = logging.getLogger(__name__)

AUTOBACKUP_DEFAULTS: dict[str, Any] = {
    "autobackup_enabled": False,
    "autobackup_interval_hours": 24,
    "autobackup_scope": "all",  # "all" public repositories or "favorites"
    "autobackup_skip_unchanged": True,
    "autobackup_keep": 0,  # automatic backups kept per repository; 0 = keep all
}
AUTOMATIC_REASON = "automatic"


@dataclass
class BulkResult:
    created: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"created {len(self.created)} · unchanged {len(self.unchanged)} · "
                f"skipped {len(self.skipped)} · failed {len(self.failed)}"
                + (f" · pruned {len(self.pruned)}" if self.pruned else ""))


async def _noop(_: str) -> None:
    return None


class BulkBackups:
    def __init__(self, services: Services) -> None:
        self.s = services
        self._running = asyncio.Lock()

    @property
    def busy(self) -> bool:
        return self._running.locked()

    async def settings(self) -> dict[str, Any]:
        return {k: await self.s.db.get_setting(k, v) for k, v in AUTOBACKUP_DEFAULTS.items()}

    async def managed_repos(self, favorites_only: bool = False) -> list[dict[str, Any]]:
        repos = [r for r in await self.s.gh.all_repos(public_only=True) if self._managed(r)]
        if favorites_only:
            favorites = {f.lower() for f in await self.s.db.list_favorites()}
            repos = [r for r in repos if r["full_name"].lower() in favorites]
        return repos

    def _managed(self, meta: dict[str, Any]) -> bool:
        try:
            self.s.policy.check_writable_meta(meta)
            return True
        except PolicyError:
            return False

    async def run(self, repos: list[dict[str, Any]], *, reason: str, skip_unchanged: bool,
                  progress: Progress = _noop) -> BulkResult:
        if self._running.locked():
            raise BackupError("Another bulk backup is already running.")
        result = BulkResult()
        async with self._running:
            for index, meta in enumerate(repos, 1):
                full_name = meta["full_name"]
                repo = RepoRef(*full_name.split("/", 1))
                await progress(f"💾 [{index}/{len(repos)}] {full_name}…")
                try:
                    await self.s.db.acquire_lock(repo.key, f"{reason} backup")
                except RepoLockedError as exc:
                    result.skipped.append(f"{full_name} (locked: {exc.holder})")
                    continue
                try:
                    if meta.get("size", 1) == 0 and not meta.get("pushed_at"):
                        result.skipped.append(f"{full_name} (empty)")
                        continue
                    if skip_unchanged:
                        latest = await self.s.db.latest_verified_backup(full_name)
                        if latest:
                            matches, _ = await self.s.backups.remote_matches(full_name, latest.id)
                            if matches:
                                result.unchanged.append(f"{full_name} ({latest.id})")
                                continue
                    record = await self.s.backups.create_verified(repo, reason)
                    result.created.append(f"{full_name} ({record.id})")
                except (BackupError, GitHubError) as exc:
                    result.failed.append(f"{full_name}: {get_redactor()(str(exc))[:200]}")
                finally:
                    await self.s.db.release_lock(repo.key)
        return result

    async def prune_automatic(self, keep: int) -> list[str]:
        """Delete old *automatic* backups beyond `keep` per repository.

        Never deletes manual or pre-operation backups, the newest verified backup of a
        repository, or a backup referenced by a pending/running operation.
        """
        if keep <= 0:
            return []
        busy = {op.backup_id for op in await self.s.db.operations_in_stages([*WAITING, *RUNNING])}
        busy |= {op.params.get("backup_id") for op in await self.s.db.operations_in_stages([*WAITING, *RUNNING])}
        records, _ = await self.s.db.list_backups(limit=100_000)
        pruned: list[str] = []
        by_repo: dict[str, list] = {}
        for record in records:
            by_repo.setdefault(record.repo.lower(), []).append(record)
        for repo_records in by_repo.values():
            newest_verified = next((r.id for r in repo_records if r.status == "verified"), None)
            automatic = [r for r in repo_records if r.reason == AUTOMATIC_REASON]
            for record in automatic[keep:]:
                if record.id in busy or record.id == newest_verified:
                    continue
                try:
                    await self.s.backups.delete(record.id)
                    pruned.append(record.id)
                except BackupError:
                    continue
        return pruned

    async def autobackup_due(self) -> bool:
        cfg = await self.settings()
        if not cfg["autobackup_enabled"] or self.busy:
            return False
        last = parse_iso(await self.s.db.get_setting("autobackup_last_run"))
        return last is None or utcnow() - last >= timedelta(hours=int(cfg["autobackup_interval_hours"]))

    async def run_autobackup(self) -> BulkResult:
        cfg = await self.settings()
        await self.s.db.set_setting("autobackup_last_run", utcnow().isoformat())
        repos = await self.managed_repos(favorites_only=cfg["autobackup_scope"] == "favorites")
        result = await self.run(repos, reason=AUTOMATIC_REASON, skip_unchanged=bool(cfg["autobackup_skip_unchanged"]))
        result.pruned = await self.prune_automatic(int(cfg["autobackup_keep"]))
        await self.s.db.log_simple("autobackup", None, "done" if not result.failed else "failed",
                                   result={"summary": result.summary()})
        return result

    async def verify_all(self, progress: Progress = _noop) -> dict[str, list[str]]:
        records, _ = await self.s.db.list_backups(limit=100_000)
        report: dict[str, list[str]] = {"ok": [], "damaged": [], "missing": []}
        for index, record in enumerate(records, 1):
            await progress(f"🔎 [{index}/{len(records)}] {record.id}…")
            try:
                directory = self.s.backups.backup_dir(record)
            except BackupError:
                report["damaged"].append(f"{record.id} (path outside BACKUP_PATH)")
                continue
            if record.status == "creating":
                continue
            if not directory.is_dir():
                report["missing"].append(f"{record.id} ({record.repo})")
                await self.s.db.update_backup(record.id, status="failed", error="backup directory missing")
                continue
            verification = await self.s.backups.verify(record.id)
            if verification.ok:
                report["ok"].append(record.id)
            else:
                report["damaged"].append(f"{record.id} ({record.repo}): {'; '.join(verification.failed())[:150]}")
        return report

    async def storage(self) -> dict[str, Any]:
        path = self.s.settings.backup_path
        usage = shutil.disk_usage(path)
        size = await asyncio.to_thread(dir_size, path)
        records, total = await self.s.db.list_backups(limit=100_000)
        by_status: dict[str, int] = {}
        for record in records:
            by_status[record.status] = by_status.get(record.status, 0) + 1
        return {"total": total, "by_status": by_status, "size": size, "free": usage.free, "disk_total": usage.total,
                "latest": records[0] if records else None,
                "last_auto": await self.s.db.get_setting("autobackup_last_run")}

    async def diff(self, backup_id: str) -> dict[str, Any]:
        """Compare a backup's branches/tags with the repository on GitHub (read-only)."""
        details = await self.s.backups.details(backup_id)
        if details is None or details.manifest is None:
            raise BackupError(f"Backup {backup_id} is not available.")
        repo = details.record.repo
        await self.s.policy.readable(RepoRef(*repo.split("/", 1)))
        backup_refs = important(details.manifest["refs"])
        remote = important(await asyncio.to_thread(ls_remote, self.s.git, self.s.backups.url_for(repo)))
        added = sorted(r for r in remote if r not in backup_refs)
        removed = sorted(r for r in backup_refs if r not in remote)
        changed = []
        for ref in sorted(r for r in backup_refs if r in remote and remote[r] != backup_refs[r]):
            entry = {"ref": ref, "backup": backup_refs[ref][:7], "github": remote[ref][:7]}
            if ref.startswith("refs/heads/"):
                try:
                    comparison = await self.s.gh.compare(repo, backup_refs[ref], remote[ref])
                    entry["ahead"] = comparison.get("ahead_by")
                    entry["behind"] = comparison.get("behind_by")
                    entry["status"] = comparison.get("status")
                except GitHubError:
                    entry["status"] = "backup commit no longer on GitHub (history rewritten?)"
            changed.append(entry)
        return {"repo": repo, "added": added, "removed": removed, "changed": changed,
                "identical": not (added or removed or changed)}
