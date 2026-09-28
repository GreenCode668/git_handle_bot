"""Backup lifecycle: create, verify, inspect, delete, and compare with GitHub."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ghbot import __version__
from ghbot.config import Settings
from ghbot.db import BackupRecord, Database, iso, utcnow
from ghbot.git.mirror import (
    VerifyReport,
    create_backup_artifacts,
    important,
    ls_remote,
    verify_backup_artifacts,
)
from ghbot.git.runner import GitError, GitRunner
from ghbot.github.api import GitHubAPI
from ghbot.logging_setup import get_redactor
from ghbot.validators import RepoRef

log = logging.getLogger(__name__)

METADATA_KEYS = (
    "name", "full_name", "description", "homepage", "private", "visibility", "default_branch",
    "topics", "has_issues", "has_projects", "has_wiki", "archived", "fork", "size", "created_at", "pushed_at",
)


class BackupError(Exception):
    """Backup creation or verification failed. GitHub must not be modified."""


def github_git_url(full_name: str) -> str:
    return f"https://github.com/{full_name}.git"


@dataclass
class BackupDetails:
    record: BackupRecord
    manifest: dict[str, Any] | None
    metadata: dict[str, Any] | None


class BackupService:
    def __init__(self, settings: Settings, db: Database, gh: GitHubAPI, git: GitRunner,
                 url_for: Callable[[str], str] = github_git_url) -> None:
        self.settings = settings
        self.db = db
        self.gh = gh
        self.git = git
        self.url_for = url_for
        self.policy = None  # RepoPolicy, injected by Services.build

    def backup_dir(self, record: BackupRecord) -> Path:
        path = Path(record.path).resolve()
        if path.parent != self.settings.backup_path:
            raise BackupError("Backup path is outside BACKUP_PATH")
        return path

    async def create(self, repo: RepoRef, reason: str, operation_id: int | None = None) -> BackupRecord:
        backup_id = await self.db.next_backup_id()
        final = self.settings.backup_path / backup_id
        tmp = self.settings.backup_path / f".tmp-{backup_id}"
        await self.db.insert_backup(backup_id, repo.full_name, str(final), reason, operation_id)
        try:
            meta = await self.gh.get_repo(repo.full_name)
            if self.policy is None:
                raise BackupError("repository policy not configured")
            self.policy.check_writable_meta(meta)  # backups are for managed (owned + public) repositories only
            await self.db.update_backup(backup_id, visibility=meta.get("visibility", "public"))
            include_wiki = await self.db.get_setting("backup_include_wiki", True)
            wiki_url = self.url_for(f"{repo.full_name}.wiki") if include_wiki and meta.get("has_wiki") else None
            artifacts = await asyncio.to_thread(
                create_backup_artifacts, self.git, self.url_for(repo.full_name), tmp, auth=True, wiki_url=wiki_url
            )
            manifest = {
                "id": backup_id,
                "repo": repo.full_name,
                "created_at": iso(utcnow()),
                "reason": reason,
                "operation_id": operation_id,
                "refs": artifacts.refs,
                "head": artifacts.head,
                "commit_count": artifacts.commit_count,
                "bundle_sha256": artifacts.bundle_sha256,
                "object_format": artifacts.object_format,
                "wiki_included": artifacts.wiki_included,
                "warnings": artifacts.warnings,
                "git_version": self.git.version(),
                "bot_version": __version__,
                "includes": ["git objects", "branches", "tags", "all refs", "repository metadata"]
                + (["wiki"] if artifacts.wiki_included else []),
                "excludes": ["issues", "pull request discussions", "releases assets", "Actions secrets/logs", "LFS objects"],
            }
            metadata = {k: meta.get(k) for k in METADATA_KEYS}
            (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
            (tmp / "metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True))
            os.replace(tmp, final)
            await self.db.update_backup(
                backup_id,
                status="created",
                ref_count=len(artifacts.refs),
                commit_count=artifacts.commit_count,
                size_bytes=artifacts.size_bytes,
                bundle_sha256=artifacts.bundle_sha256,
            )
        except Exception as exc:  # noqa: BLE001
            shutil.rmtree(tmp, ignore_errors=True)
            message = get_redactor()(str(exc))
            await self.db.update_backup(backup_id, status="failed", error=message[:1000])
            log.error("Backup %s for %s failed: %s", backup_id, repo.full_name, message)
            raise BackupError(f"Backup {backup_id} failed: {message}") from None
        record = await self.db.get_backup(backup_id)
        assert record is not None
        return record

    async def verify(self, backup_id: str) -> VerifyReport:
        record = await self.db.get_backup(backup_id)
        if record is None or record.status in ("deleted", "creating"):
            raise BackupError(f"Backup {backup_id} is not available for verification")
        try:
            report = await asyncio.to_thread(
                verify_backup_artifacts, self.git, self.backup_dir(record), self.settings.work_path
            )
        except (GitError, OSError) as exc:
            report = VerifyReport(False, [("verification run", False, get_redactor()(str(exc)))])
        if report.ok:
            await self.db.update_backup(backup_id, status="verified", verified_at=utcnow(), error=None)
        else:
            await self.db.update_backup(backup_id, status="failed", error="; ".join(report.failed())[:1000])
        return report

    async def create_verified(self, repo: RepoRef, reason: str, operation_id: int | None = None) -> BackupRecord:
        record = await self.create(repo, reason, operation_id)
        report = await self.verify(record.id)
        if not report.ok:
            raise BackupError(f"Backup {record.id} failed verification: " + "; ".join(report.failed()))
        refreshed = await self.db.get_backup(record.id)
        assert refreshed is not None
        return refreshed

    async def details(self, backup_id: str) -> BackupDetails | None:
        record = await self.db.get_backup(backup_id)
        if record is None:
            return None
        manifest = metadata = None
        if record.status != "deleted":
            try:
                directory = self.backup_dir(record)
                manifest = json.loads((directory / "manifest.json").read_text())
                metadata = json.loads((directory / "metadata.json").read_text())
            except (OSError, ValueError, BackupError):
                pass
        return BackupDetails(record, manifest, metadata)

    async def delete(self, backup_id: str) -> None:
        record = await self.db.get_backup(backup_id)
        if record is None or record.status == "deleted":
            raise BackupError("Backup not found")
        directory = self.backup_dir(record)
        await asyncio.to_thread(shutil.rmtree, directory, True)
        await self.db.update_backup(backup_id, status="deleted")

    async def remote_matches(self, full_name: str, backup_id: str) -> tuple[bool, str]:
        """True when GitHub's branches/tags are exactly what the backup captured."""
        details = await self.details(backup_id)
        if not details or not details.manifest:
            return False, "backup manifest unavailable"
        try:
            remote = await asyncio.to_thread(ls_remote, self.git, self.url_for(full_name))
        except GitError as exc:
            return False, str(exc)
        expected = important(details.manifest["refs"])
        actual = important(remote)
        if actual == expected:
            return True, "remote matches backup"
        changed = sorted(set(expected.items()) ^ set(actual.items()))
        return False, f"{len(changed)} ref difference(s) between GitHub and the backup"

    async def cleanup_temp(self) -> None:
        for path in self.settings.backup_path.glob(".tmp-BK-*"):
            shutil.rmtree(path, ignore_errors=True)
        for record in (await self.db.list_backups(limit=1000))[0]:
            if record.status == "creating":
                await self.db.update_backup(record.id, status="failed", error="interrupted by restart")
