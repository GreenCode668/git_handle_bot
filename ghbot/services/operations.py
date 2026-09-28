"""Concrete operations run through the safety engine."""

from __future__ import annotations

import asyncio
import json
import secrets
import shutil
from datetime import date
from pathlib import Path
from typing import Any, ClassVar

from ghbot.db import Operation
from ghbot.git.history import (
    RefUpdate,
    analyze_history,
    author_stats,
    build_push_args,
    rewrite_authors,
    rewrite_history,
)
from ghbot.git.mirror import important, local_refs, ls_remote, mirror_clone, remote_head
from ghbot.github.client import GitHubNotFound
from ghbot.services.safety import Impact, OperationSpec, Progress, SafetyError
from ghbot.validators import (
    RepoRef,
    ValidationError,
    cutoff_datetime,
    validate_backup_id,
    validate_description,
    validate_git_url,
    validate_homepage,
    validate_repo_name,
    validate_topics,
)

# Chosen by the user: removed pre-cutoff commits are not replaced by a summary commit.
DEFAULT_SQUASH_STYLE = "none"


def _fmt_size(kb: int | None) -> str:
    kb = kb or 0
    return f"{kb / 1024:.1f} MB" if kb >= 1024 else f"{kb} KB"


async def _get_repo_or_none(spec: OperationSpec, repo: RepoRef) -> dict[str, Any] | None:
    try:
        return await spec.s.gh.get_repo(repo.full_name)
    except GitHubNotFound:
        return None


class _WorkDir:
    """A disposable directory under WORK_PATH, always removed afterwards."""

    def __init__(self, spec: OperationSpec, name: str) -> None:
        self.path = spec.s.settings.work_path / f"{name}-{secrets.token_hex(4)}"

    async def __aenter__(self) -> Path:
        await asyncio.to_thread(shutil.rmtree, self.path, True)
        self.path.mkdir(parents=True)
        return self.path

    async def __aexit__(self, *exc: object) -> None:
        await asyncio.to_thread(shutil.rmtree, self.path, True)


# ------------------------------------------------------------------ delete
class DeleteRepository(OperationSpec):
    kind = "delete_repo"
    verb = "DELETE"
    label = "repository deletion"

    async def analyze(self, repo: RepoRef, params: dict[str, Any], progress: Progress) -> Impact:
        meta = await _get_repo_or_none(self, repo)
        if meta is None:
            return Impact("Delete repository", [], blockers=[f"{repo} does not exist."], target_exists=False)
        pulls = await self.s.gh.count_open_pulls(repo.full_name)
        return Impact(
            title=f"Delete repository {repo}",
            lines=[
                f"Visibility: {meta['visibility']}{' (archived)' if meta['archived'] else ''}",
                f"Default branch: {meta.get('default_branch')}",
                f"Size: {_fmt_size(meta.get('size'))}",
                f"Stars: {meta['stargazers_count']} · Forks: {meta['forks_count']} · Watchers: {meta['subscribers_count'] if 'subscribers_count' in meta else meta.get('watchers_count')}",
                f"Open issues+PRs: {meta['open_issues_count']} (open PRs: {pulls})",
            ],
            warnings=[
                "The repository will be permanently deleted from GitHub.",
                "A full git mirror backup (all branches, tags, refs, history) is created first.",
                "NOT recoverable from a git backup: issues, pull request discussions, releases, "
                "stars, watchers, Actions secrets, logs and settings.",
            ],
            data={"id": meta["id"], "private": meta["private"]},
        )

    async def preflight(self, op: Operation, repo: RepoRef) -> None:
        meta = await _get_repo_or_none(self, repo)
        if meta is None or meta["id"] != (op.impact or {}).get("data", {}).get("id"):
            raise SafetyError("Repository no longer matches the analyzed repository. Nothing was changed.")

    async def execute(self, op: Operation, repo: RepoRef, progress: Progress) -> dict[str, Any]:
        await self.s.gh.delete_repo(repo.full_name)
        return {"deleted": repo.full_name, "backup_id": op.backup_id}

    async def verify(self, op: Operation, repo: RepoRef, result: dict[str, Any]) -> list[str]:
        for _ in range(6):
            if not await self.s.gh.repo_exists(repo.full_name):
                return []
            await asyncio.sleep(2)
        return ["repository still exists on GitHub"]


# ------------------------------------------------------------ history rewrite
class RewriteHistory(OperationSpec):
    kind = "rewrite_history"
    verb = "REWRITE"
    label = "history rewrite"

    async def analyze(self, repo: RepoRef, params: dict[str, Any], progress: Progress) -> Impact:
        cutoff = cutoff_datetime(date.fromisoformat(params["cutoff"]))
        policy = params.get("tag_policy", "snapshot")
        style = params.get("squash_style", "summary")
        meta = await _get_repo_or_none(self, repo)
        if meta is None:
            return Impact("History analysis", [], blockers=[f"{repo} does not exist."], target_exists=False)

        await progress("📥 Cloning a read-only analysis copy…")
        async with _WorkDir(self, "analyze") as work:
            mirror = work / "repo.git"
            await asyncio.to_thread(mirror_clone, self.s.git, self.s.backups.url_for(repo.full_name), mirror)
            await progress("🔬 Analyzing commit graph…")
            analysis = await asyncio.to_thread(analyze_history, self.s.git, mirror, cutoff, policy, style)

        blockers = list(analysis.blockers)
        warnings = list(analysis.complications)
        if meta.get("archived"):
            blockers.append("Repository is archived (read-only). Unarchive it first.")
        affected = {b.name for b in analysis.affected_branches}
        protected = await self.s.gh.list_branches(repo.full_name, per_page=100, protected=True)
        blocked_branches = sorted(affected & {b["name"] for b in protected.items})
        if blocked_branches:
            warnings.append(
                "Protected branches affected: " + ", ".join(blocked_branches)
                + ". The atomic push will be rejected unless force pushes are allowed."
            )
        scopes = self.s.gh.client.last_scopes
        if analysis.touches_workflows and scopes is not None and "workflow" not in scopes:
            blockers.append("Token lacks the 'workflow' scope required to push rewritten workflow files.")
        if not analysis.rewrite_required and not analysis.blockers:
            blockers.append("No history rewrite is required for this cutoff.")

        a = analysis
        lines = [
            f"Cutoff: {params['cutoff']} 00:00 UTC",
            f"Total commits: {a.total_commits}",
            f"Commits before cutoff (by date): {a.dated_before}",
            f"Commits after cutoff (by date): {a.dated_after}",
            *((f"Commits squashed into a summary commit: {a.squash_count}",
               f"Commits rewritten (content kept): {a.rewrite_count}",
               f"Summary commits created: {a.boundary_count}")
              if style == "summary" else
              (f"Commits removed: {a.squash_count} (no summary commit)",
               f"Commits kept and rewritten (content kept): {a.rewrite_count}",
               f"Commits after rewrite: {a.rewrite_count + a.snapshot_count}",
               f"Snapshot commits needed (branches/tags with no newer commits): {a.snapshot_count}")),
            f"Merges: {a.merges_total} (crossing cutoff: {a.merges_crossing_cutoff})",
            f"Affected branches: {', '.join(sorted(affected)) or 'none'}",
            f"Affected tags: {', '.join(t.name + ' (' + t.status + ')' for t in a.affected_tags) or 'none'}",
            f"Old-tag policy: {'keep as snapshots' if policy == 'snapshot' else 'delete old tags'}",
            f"History rewrite required: {'YES' if a.rewrite_required else 'no'}",
        ]
        return Impact(
            title=f"History analysis for {repo}",
            lines=lines,
            warnings=warnings,
            blockers=blockers,
            data={"analysis": a.to_dict(),
                  "strategy": "tree-preserving cutoff squash" if style == "summary" else "tree-preserving cutoff removal (no summary commit)"},
        )

    async def execute(self, op: Operation, repo: RepoRef, progress: Progress) -> dict[str, Any]:
        record = await self.s.db.get_backup(op.backup_id or "")
        if record is None:
            raise SafetyError("Backup record missing")
        source = self.s.backups.backup_dir(record) / "repo.git"
        cutoff = cutoff_datetime(date.fromisoformat(op.params["cutoff"]))
        policy = op.params.get("tag_policy", "snapshot")
        style = op.params.get("squash_style", "summary")
        async with _WorkDir(self, f"rewrite-op-{op.id}") as work:
            mirror = work / "repo.git"
            # Work from a copy of the verified backup so leases pin to exactly the backed-up SHAs.
            await asyncio.to_thread(mirror_clone, self.s.git, str(source), mirror, auth=False, local=True)
            await progress("✂️ Rewriting history locally and verifying file trees…")
            result = await asyncio.to_thread(rewrite_history, self.s.git, mirror, cutoff, policy, style)
            manifest = json.loads((source.parent / "manifest.json").read_text())
            for update in result.updates:
                if manifest["refs"].get(update.ref) != update.old:
                    raise SafetyError(f"Ref {update.ref} does not match the backup. Nothing was pushed.")
            await progress(f"🚀 Atomic force-with-lease push of {len(result.updates)} ref(s)…")
            await asyncio.to_thread(
                self.s.git.run, build_push_args(self.s.backups.url_for(repo.full_name), result.updates), cwd=mirror, auth=True
            )
        return {
            "updated_refs": len(result.updates),
            "commits_squashed": result.removed,
            "commits_rewritten": result.rewritten,
            "squash_roots": result.squash_roots,
            "expected_refs": result.expected_refs,
            "backup_id": op.backup_id,
        }

    async def verify(self, op: Operation, repo: RepoRef, result: dict[str, Any]) -> list[str]:
        remote = important(await asyncio.to_thread(ls_remote, self.s.git, self.s.backups.url_for(repo.full_name)))
        if remote != result["expected_refs"]:
            diff = set(remote.items()) ^ set(result["expected_refs"].items())
            return [f"GitHub refs differ from the verified local rewrite ({len(diff)} differences)"]
        return []


# ------------------------------------------------------------------ restore
class RestoreBackup(OperationSpec):
    kind = "restore_backup"
    verb = "RESTORE"
    label = "backup restore"
    may_create = True

    async def creation_private(self, repo: RepoRef, params: dict[str, Any]) -> bool:
        details = await self._source(params)
        return (details.metadata or {}).get("private", True) is not False

    async def _source(self, params: dict[str, Any]):
        backup_id = validate_backup_id(params["backup_id"])
        details = await self.s.backups.details(backup_id)
        if details is None or details.manifest is None:
            raise SafetyError(f"Backup {backup_id} is not available.")
        return details

    async def analyze(self, repo: RepoRef, params: dict[str, Any], progress: Progress) -> Impact:
        details = await self._source(params)
        await progress(f"🔎 Verifying source backup {details.record.id}…")
        report = await self.s.backups.verify(details.record.id)
        blockers = [] if report.ok else [f"Source backup failed verification: {'; '.join(report.failed())}"]
        backup_refs = important(details.manifest["refs"])
        meta = await _get_repo_or_none(self, repo)
        lines = [
            f"Source backup: {details.record.id} ({details.record.repo}, {details.record.created_at:%Y-%m-%d %H:%M} UTC)",
            f"Target: {repo}",
            f"Backup branches/tags: {len(backup_refs)} · commits: {details.manifest['commit_count']}",
        ]
        warnings = []
        if meta is None:
            lines.append("Target does not exist: it will be created as a PUBLIC repository.")
            warnings.append("Issues, PRs, releases and settings are not part of git backups and cannot be restored.")
        else:
            if meta.get("archived"):
                blockers.append("Target repository is archived. Unarchive it first.")
            remote = important(await asyncio.to_thread(ls_remote, self.s.git, self.s.backups.url_for(repo.full_name)))
            create = [r for r in backup_refs if r not in remote]
            update = [r for r in backup_refs if r in remote and remote[r] != backup_refs[r]]
            delete = [r for r in remote if r not in backup_refs]
            lines += [
                f"Refs to create: {len(create)}",
                f"Refs to force-update: {len(update)}",
                f"Refs to delete (not in backup): {len(delete)}",
            ]
            if not (create or update or delete):
                blockers.append("Target already matches the backup; nothing to restore.")
            warnings.append("This overwrites current branches and tags. A safety backup of the current state is taken first.")
        return Impact(
            title=f"Restore {details.record.id} into {repo}",
            lines=lines, warnings=warnings, blockers=blockers,
            target_exists=meta is not None,
            data={"backup_id": details.record.id, "target_id": meta["id"] if meta else None},
        )

    async def preflight(self, op: Operation, repo: RepoRef) -> None:
        details = await self._source(op.params)
        report = await self.s.backups.verify(details.record.id)
        if not report.ok:
            raise SafetyError("Source backup failed verification. Nothing was changed.")
        meta = await _get_repo_or_none(self, repo)
        expected_id = (op.impact or {}).get("data", {}).get("target_id")
        if (meta["id"] if meta else None) != expected_id:
            raise SafetyError("Target repository changed (created/deleted) since analysis. Nothing was changed.")

    async def execute(self, op: Operation, repo: RepoRef, progress: Progress) -> dict[str, Any]:
        details = await self._source(op.params)
        metadata = details.metadata or {}
        backup_refs = important(details.manifest["refs"])
        url = self.s.backups.url_for(repo.full_name)
        created = False
        if not (op.impact or {}).get("target_exists"):
            await progress("🆕 Creating repository…")
            await self.s.gh.create_repo(
                repo.name, private=False, description=metadata.get("description"),
                homepage=metadata.get("homepage"), has_issues=metadata.get("has_issues", True),
                has_wiki=metadata.get("has_wiki", True), has_projects=metadata.get("has_projects", True),
            )
            created = True
            if metadata.get("topics"):
                await self.s.gh.replace_topics(repo.full_name, metadata["topics"])
            await asyncio.sleep(2)

        # Leases: expected current values come from the safety backup (verified to match GitHub).
        current: dict[str, str] = {}
        if op.backup_id:
            safety = await self.s.backups.details(op.backup_id)
            current = important(safety.manifest["refs"]) if safety and safety.manifest else {}

        source = self.s.backups.backup_dir(details.record) / "repo.git"
        async with _WorkDir(self, f"restore-op-{op.id}") as work:
            mirror = work / "repo.git"
            await asyncio.to_thread(mirror_clone, self.s.git, str(source), mirror, auth=False, local=True)
            upserts = [RefUpdate(r, current.get(r), sha) for r, sha in backup_refs.items() if current.get(r) != sha]
            if upserts:
                await progress(f"🚀 Pushing {len(upserts)} ref(s) from backup…")
                await asyncio.to_thread(self.s.git.run, build_push_args(url, upserts), cwd=mirror, auth=True)

            head = details.manifest.get("head")
            if head and head.startswith("refs/heads/") and head in backup_refs:
                branch = head.removeprefix("refs/heads/")
                await self.s.gh.update_repo(repo.full_name, default_branch=branch)

            deletes = [RefUpdate(r, sha, None) for r, sha in current.items() if r not in backup_refs]
            if deletes:
                await progress(f"🧹 Removing {len(deletes)} ref(s) not present in the backup…")
                await asyncio.to_thread(self.s.git.run, build_push_args(url, deletes), cwd=mirror, auth=True)

        return {
            "restored_from": details.record.id,
            "created_repo": created,
            "refs_pushed": len(upserts),
            "refs_deleted": len(deletes),
            "expected_refs": backup_refs,
            "safety_backup": op.backup_id,
        }

    async def verify(self, op: Operation, repo: RepoRef, result: dict[str, Any]) -> list[str]:
        remote = important(await asyncio.to_thread(ls_remote, self.s.git, self.s.backups.url_for(repo.full_name)))
        if remote != result["expected_refs"]:
            return [f"GitHub refs differ from backup ({len(set(remote.items()) ^ set(result['expected_refs'].items()))} differences)"]
        return []


# ------------------------------------------------------- simple repo changes
class RenameRepository(OperationSpec):
    kind = "rename_repo"
    verb = "RENAME"
    label = "rename"
    requires_backup = False
    confirm_mode = "button"

    async def analyze(self, repo: RepoRef, params: dict[str, Any], progress: Progress) -> Impact:
        new_name = validate_repo_name(params["new_name"])
        meta = await _get_repo_or_none(self, repo)
        blockers = [] if meta else [f"{repo} does not exist."]
        if new_name.lower() != repo.name.lower() and await self.s.gh.repo_exists(f"{repo.owner}/{new_name}"):
            blockers.append(f"{repo.owner}/{new_name} already exists.")
        if new_name == repo.name:
            blockers.append("New name is the same as the current name.")
        return Impact(
            f"Rename {repo}", [f"{repo.name} → {new_name}"],
            warnings=["GitHub redirects the old URL, but update local remotes and CI references."],
            blockers=blockers,
        )

    async def execute(self, op: Operation, repo: RepoRef, progress: Progress) -> dict[str, Any]:
        data = await self.s.gh.update_repo(repo.full_name, name=op.params["new_name"])
        return {"full_name": data["full_name"]}

    async def verify(self, op: Operation, repo: RepoRef, result: dict[str, Any]) -> list[str]:
        ok = await self.s.gh.repo_exists(f"{repo.owner}/{op.params['new_name']}")
        return [] if ok else ["renamed repository not found"]


class _SetRepoField(OperationSpec):
    requires_backup = False
    confirm_mode = "button"
    field: str
    value: Any
    description: str

    async def analyze(self, repo: RepoRef, params: dict[str, Any], progress: Progress) -> Impact:
        meta = await _get_repo_or_none(self, repo)
        if meta is None:
            return Impact(self.label, [], blockers=[f"{repo} does not exist."], target_exists=False)
        current = meta.get(self.field)
        blockers = [f"Already {self.description}."] if current == self.value else []
        return Impact(
            f"{self.label.capitalize()}: {repo}",
            [f"{self.field}: {current} → {self.value}"],
            warnings=self.warnings(meta), blockers=blockers,
        )

    def warnings(self, meta: dict[str, Any]) -> list[str]:
        return []

    async def execute(self, op: Operation, repo: RepoRef, progress: Progress) -> dict[str, Any]:
        data = await self.s.gh.update_repo(repo.full_name, **{self.field: self.value})
        return {self.field: data.get(self.field)}

    async def verify(self, op: Operation, repo: RepoRef, result: dict[str, Any]) -> list[str]:
        meta = await self.s.gh.get_repo(repo.full_name)
        return [] if meta.get(self.field) == self.value else [f"{self.field} is still {meta.get(self.field)}"]


class MakePrivate(_SetRepoField):
    kind, verb, label, field, value, description = "make_private", "PRIVATE", "make private", "private", True, "private"

    def warnings(self, meta: dict[str, Any]) -> list[str]:
        return ["Stars and watchers from users without access may be lost; public forks are detached.",
                "After this the bot can NO LONGER manage this repository (public-only policy), including undo."]


class MakePublic(_SetRepoField):
    kind, verb, label, field, value, description = "make_public", "PUBLIC", "make public", "private", False, "public"
    confirm_mode = "phrase"

    def warnings(self, meta: dict[str, Any]) -> list[str]:
        return ["⚠️ ALL code and full commit history become visible to everyone. Check for secrets in history first."]


class ArchiveRepository(_SetRepoField):
    kind, verb, label, field, value, description = "archive_repo", "ARCHIVE", "archive", "archived", True, "archived"

    def warnings(self, meta: dict[str, Any]) -> list[str]:
        return ["Archived repositories are read-only (no pushes, issues or PRs)."]


class UnarchiveRepository(_SetRepoField):
    kind, verb, label, field, value, description = "unarchive_repo", "UNARCHIVE", "unarchive", "archived", False, "not archived"


# ------------------------------------------------------------------- import
class ImportRepository(OperationSpec):
    kind = "import_repo"
    verb = "IMPORT"
    label = "import"
    requires_backup = False  # target does not exist yet; nothing can be damaged
    confirm_mode = "button"
    may_create = True

    async def creation_private(self, repo: RepoRef, params: dict[str, Any]) -> bool:
        return bool(params.get("private", False))

    async def analyze(self, repo: RepoRef, params: dict[str, Any], progress: Progress) -> Impact:
        url = validate_git_url(params["url"])
        info = await self.inspect_source(url)
        blockers = []
        if await self.s.gh.repo_exists(repo.full_name):
            blockers.append(f"{repo} already exists. Choose another name.")
        if not info["refs"]:
            blockers.append("Source repository has no branches or tags.")
        return Impact(
            f"Import into {repo}",
            [
                f"Source: {url}",
                f"Branches: {info['branches']} · Tags: {info['tags']} · Other refs (ignored): {info['other']}",
                f"Default branch: {info['head'] or 'unknown'}",
                f"Destination: {repo} (public)",
            ],
            warnings=["Full history, branches and tags are copied. Issues/PRs/releases are not."],
            blockers=blockers,
            target_exists=False,
            data={"source_refs": info["refs"], "head": info["head"]},
        )

    async def inspect_source(self, url: str) -> dict[str, Any]:
        is_github = url.lower().startswith("https://github.com/")
        refs = await asyncio.to_thread(ls_remote, self.s.git, url, auth=is_github)
        head = await asyncio.to_thread(remote_head, self.s.git, url, auth=is_github)
        imp = important(refs)
        return {
            "refs": imp,
            "branches": sum(1 for r in imp if r.startswith("refs/heads/")),
            "tags": sum(1 for r in imp if r.startswith("refs/tags/")),
            "other": len(refs) - len(imp),
            "head": head,
        }

    async def preflight(self, op: Operation, repo: RepoRef) -> None:
        if await self.s.gh.repo_exists(repo.full_name):
            raise SafetyError(f"{repo} was created meanwhile. Nothing was changed.")

    async def execute(self, op: Operation, repo: RepoRef, progress: Progress) -> dict[str, Any]:
        url = validate_git_url(op.params["url"])
        is_github = url.lower().startswith("https://github.com/")
        async with _WorkDir(self, f"import-op-{op.id}") as work:
            mirror = work / "repo.git"
            await progress("📥 Mirror-cloning source…")
            await asyncio.to_thread(mirror_clone, self.s.git, url, mirror, auth=is_github)
            refs = important(await asyncio.to_thread(local_refs, self.s.git, mirror))
            await progress("🆕 Creating destination repository…")
            await self.s.gh.create_repo(repo.name, private=False)
            await asyncio.sleep(2)
            await progress(f"🚀 Pushing {len(refs)} branch(es)/tag(s)…")
            updates = [RefUpdate(r, None, sha) for r, sha in refs.items()]
            try:
                await asyncio.to_thread(
                    self.s.git.run, build_push_args(self.s.backups.url_for(repo.full_name), updates), cwd=mirror, auth=True
                )
            except Exception as exc:
                raise SafetyError(
                    f"Push failed after creating {repo} (it may be empty; remove it with '{repo.name} @remove'): {exc}"
                ) from None
        head = (op.impact or {}).get("data", {}).get("head")
        if head and head.startswith("refs/heads/") and head in refs:
            await self.s.gh.update_repo(repo.full_name, default_branch=head.removeprefix("refs/heads/"))
        return {"expected_refs": refs, "url": f"https://github.com/{repo.full_name}"}

    async def verify(self, op: Operation, repo: RepoRef, result: dict[str, Any]) -> list[str]:
        remote = important(await asyncio.to_thread(ls_remote, self.s.git, self.s.backups.url_for(repo.full_name)))
        missing = set(result["expected_refs"].items()) - set(remote.items())
        problems = [f"{len(missing)} ref(s) missing on GitHub"] if missing else []
        meta = await self.s.gh.get_repo(repo.full_name)
        if meta.get("private") is not False:
            problems.append("imported repository is not public")
        return problems


# ------------------------------------------------------------ create repository
class CreateRepository(OperationSpec):
    kind = "create_repo"
    verb = "CREATE"
    label = "create repository"
    requires_backup = False
    confirm_mode = "button"
    may_create = True

    async def creation_private(self, repo: RepoRef, params: dict[str, Any]) -> bool:
        return False  # this operation only ever creates public repositories

    async def analyze(self, repo: RepoRef, params: dict[str, Any], progress: Progress) -> Impact:
        validate_repo_name(repo.name)
        description = validate_description(params.get("description") or "")
        blockers = [f"{repo} already exists."] if await self.s.gh.repo_exists(repo.full_name) else []
        return Impact(
            f"Create repository {repo}",
            [f"Name: {repo.name}", "Visibility: PUBLIC", f"Description: {description or '—'}",
             "Initialized empty (no README, license or .gitignore)."],
            warnings=["Everything pushed to this repository will be publicly visible."],
            blockers=blockers, target_exists=False,
        )

    async def preflight(self, op: Operation, repo: RepoRef) -> None:
        if await self.s.gh.repo_exists(repo.full_name):
            raise SafetyError(f"{repo} was created meanwhile. Nothing was changed.")

    async def execute(self, op: Operation, repo: RepoRef, progress: Progress) -> dict[str, Any]:
        data = await self.s.gh.create_repo(repo.name, private=False, description=op.params.get("description") or None)
        return {"full_name": data["full_name"], "clone_url": data.get("clone_url")}

    async def verify(self, op: Operation, repo: RepoRef, result: dict[str, Any]) -> list[str]:
        meta = await _get_repo_or_none(self, repo)
        if meta is None:
            return ["repository not found after creation"]
        return [] if meta.get("private") is False else ["repository is not public"]


# ---------------------------------------------------- description / homepage / topics
class _SetText(OperationSpec):
    requires_backup = False
    confirm_mode = "button"
    field: ClassVar[str]

    def normalize(self, value: str) -> str:
        raise NotImplementedError

    async def analyze(self, repo: RepoRef, params: dict[str, Any], progress: Progress) -> Impact:
        meta = await _get_repo_or_none(self, repo)
        if meta is None:
            return Impact(self.label, [], blockers=[f"{repo} does not exist."], target_exists=False)
        new = self.normalize(params.get("value") or "")
        old = meta.get(self.field) or ""
        return Impact(
            f"Change {self.field} of {repo}", [f"Old: {old or '—'}", f"New: {new or '—'}"],
            blockers=["New value is the same as the current value."] if old == new else [],
        )

    async def execute(self, op: Operation, repo: RepoRef, progress: Progress) -> dict[str, Any]:
        data = await self.s.gh.update_repo(repo.full_name, **{self.field: self.normalize(op.params.get("value") or "")})
        return {self.field: data.get(self.field) or ""}

    async def verify(self, op: Operation, repo: RepoRef, result: dict[str, Any]) -> list[str]:
        meta = await self.s.gh.get_repo(repo.full_name)
        expected = self.normalize(op.params.get("value") or "")
        return [] if (meta.get(self.field) or "") == expected else [f"{self.field} was not updated"]


class SetDescription(_SetText):
    kind, verb, label, field = "set_description", "DESCRIPTION", "change description", "description"

    def normalize(self, value: str) -> str:
        return validate_description(value)


class SetHomepage(_SetText):
    kind, verb, label, field = "set_homepage", "HOMEPAGE", "change homepage", "homepage"

    def normalize(self, value: str) -> str:
        return validate_homepage(value)


class SetTopics(OperationSpec):
    kind = "set_topics"
    verb = "TOPICS"
    label = "change topics"
    requires_backup = False
    confirm_mode = "button"

    async def analyze(self, repo: RepoRef, params: dict[str, Any], progress: Progress) -> Impact:
        meta = await _get_repo_or_none(self, repo)
        if meta is None:
            return Impact(self.label, [], blockers=[f"{repo} does not exist."], target_exists=False)
        old = sorted(meta.get("topics") or [])
        new = sorted(validate_topics(params.get("topics") or []))
        added, removed = sorted(set(new) - set(old)), sorted(set(old) - set(new))
        return Impact(
            f"Change topics of {repo}",
            [f"Old: {', '.join(old) or '—'}", f"New: {', '.join(new) or '—'}",
             f"Added: {', '.join(added) or '—'}", f"Removed: {', '.join(removed) or '—'}"],
            blockers=["Topics are unchanged."] if old == new else [],
        )

    async def execute(self, op: Operation, repo: RepoRef, progress: Progress) -> dict[str, Any]:
        topics = validate_topics(op.params.get("topics") or [])
        await self.s.gh.replace_topics(repo.full_name, topics)
        return {"topics": ", ".join(sorted(topics)) or "—"}

    async def verify(self, op: Operation, repo: RepoRef, result: dict[str, Any]) -> list[str]:
        meta = await self.s.gh.get_repo(repo.full_name)
        expected = sorted(validate_topics(op.params.get("topics") or []))
        return [] if sorted(meta.get("topics") or []) == expected else ["topics differ from the requested list"]


# ------------------------------------------------------------ GitHub Actions
class _RunAction(OperationSpec):
    requires_backup = False
    confirm_mode = "button"
    allowed_statuses: ClassVar[tuple[str, ...]] = ()

    def possible(self, run: dict[str, Any]) -> bool:
        raise NotImplementedError

    async def analyze(self, repo: RepoRef, params: dict[str, Any], progress: Progress) -> Impact:
        run = await self.s.gh.get_run(repo.full_name, int(params["run_id"]))
        blockers = [] if self.possible(run) else [
            f"Not possible for a run with status {run['status']} / conclusion {run.get('conclusion')}."]
        return Impact(
            f"{self.label.capitalize()}: {run['name']} #{run['run_number']}",
            [f"Repository: {repo}", f"Run id: {run['id']} · attempt {run.get('run_attempt', 1)}",
             f"Status: {run['status']} · conclusion: {run.get('conclusion') or '—'}",
             f"Branch: {run.get('head_branch')} · commit {str(run.get('head_sha') or '')[:7]}"],
            blockers=blockers, data={"attempt": run.get("run_attempt", 1)},
        )

    async def preflight(self, op: Operation, repo: RepoRef) -> None:
        run = await self.s.gh.get_run(repo.full_name, int(op.params["run_id"]))
        if not self.possible(run):
            raise SafetyError(f"Run status changed to {run['status']}; nothing was changed.")


def _run_active(run: dict[str, Any]) -> bool:
    return run["status"] in ("in_progress", "queued", "waiting", "requested", "pending")


class RerunWorkflow(_RunAction):
    kind, verb, label = "actions_rerun", "RERUN", "re-run workflow"

    def possible(self, run: dict[str, Any]) -> bool:
        return not _run_active(run)

    async def execute(self, op: Operation, repo: RepoRef, progress: Progress) -> dict[str, Any]:
        await self.s.gh.rerun(repo.full_name, int(op.params["run_id"]))
        return {"run_id": op.params["run_id"], "requested": "re-run all jobs"}

    async def verify(self, op: Operation, repo: RepoRef, result: dict[str, Any]) -> list[str]:
        previous = (op.impact or {}).get("data", {}).get("attempt", 1)
        for _ in range(5):
            run = await self.s.gh.get_run(repo.full_name, int(op.params["run_id"]))
            if run.get("run_attempt", 1) > previous or _run_active(run):
                return []
            await asyncio.sleep(3)
        return ["GitHub accepted the request but no new attempt is visible yet"]


class RerunFailedJobs(RerunWorkflow):
    kind, verb, label = "actions_rerun_failed", "RERUN", "re-run failed jobs"

    def possible(self, run: dict[str, Any]) -> bool:
        return not _run_active(run) and run.get("conclusion") in ("failure", "cancelled", "timed_out")

    async def execute(self, op: Operation, repo: RepoRef, progress: Progress) -> dict[str, Any]:
        await self.s.gh.rerun_failed(repo.full_name, int(op.params["run_id"]))
        return {"run_id": op.params["run_id"], "requested": "re-run failed jobs"}


class CancelWorkflowRun(_RunAction):
    kind, verb, label = "actions_cancel", "CANCEL", "cancel workflow run"

    def possible(self, run: dict[str, Any]) -> bool:
        return _run_active(run)

    async def execute(self, op: Operation, repo: RepoRef, progress: Progress) -> dict[str, Any]:
        await self.s.gh.cancel_run(repo.full_name, int(op.params["run_id"]))
        return {"run_id": op.params["run_id"], "requested": "cancel"}

    async def verify(self, op: Operation, repo: RepoRef, result: dict[str, Any]) -> list[str]:
        for _ in range(8):
            run = await self.s.gh.get_run(repo.full_name, int(op.params["run_id"]))
            if not _run_active(run):
                return []
            await asyncio.sleep(3)
        return ["cancellation was accepted but the run is still active after ~25s"]


# ------------------------------------------------------------------ authorship
class ReauthorRepository(OperationSpec):
    kind = "reauthor_repo"
    verb = "REAUTHOR"
    label = "author rewrite"

    @staticmethod
    def identities(params: dict[str, Any]) -> set[tuple[str, str]]:
        return {(name, email.lower()) for name, email in params.get("identities", [])}

    async def analyze(self, repo: RepoRef, params: dict[str, Any], progress: Progress) -> Impact:
        target_name = params["new_name"]
        target_email = params["new_email"]
        identities = self.identities(params)
        meta = await _get_repo_or_none(self, repo)
        if meta is None:
            return Impact("Author rewrite", [], blockers=[f"{repo} does not exist."], target_exists=False)

        await progress("📥 Cloning a read-only analysis copy…")
        async with _WorkDir(self, "reauthor-analyze") as work:
            mirror = work / "repo.git"
            await asyncio.to_thread(mirror_clone, self.s.git, self.s.backups.url_for(repo.full_name), mirror)
            await progress("🔬 Reading authors…")
            stats = await asyncio.to_thread(author_stats, self.s.git, mirror)

        total = sum(a.commits for a in stats)
        matched = [a for a in stats if (a.name, a.email.lower()) in identities]
        blockers = []
        if meta.get("archived"):
            blockers.append("Repository is archived (read-only). Unarchive it first.")
        if not matched:
            blockers.append("No commits use the selected identities.")
        protected = await self.s.gh.list_branches(repo.full_name, per_page=100, protected=True)
        warnings = [
            "Every commit is re-created, so ALL commit SHAs change (a force push).",
            "GitHub counts the rewritten commits as contributions again, on their original dates.",
            "Commit signatures are dropped; anyone with a clone must re-clone.",
            "Only remap identities that are yours: licences and attribution rules apply to other people's commits.",
        ]
        if protected.items:
            warnings.append("Protected branches: " + ", ".join(b["name"] for b in protected.items))
        return Impact(
            title=f"Rewrite commit authors in {repo}",
            lines=[
                f"Commits in history: {total}",
                f"Commits to re-author: {sum(a.commits for a in matched)}",
                *[f"  {a.name} <{a.email}> — {a.commits} commit(s)" for a in matched[:10]],
                f"New identity: {target_name} <{target_email}>",
                "File contents, dates and messages are unchanged.",
            ],
            warnings=warnings, blockers=blockers,
            data={"matched": sum(a.commits for a in matched), "total": total},
        )

    async def execute(self, op: Operation, repo: RepoRef, progress: Progress) -> dict[str, Any]:
        record = await self.s.db.get_backup(op.backup_id or "")
        if record is None:
            raise SafetyError("Backup record missing")
        source = self.s.backups.backup_dir(record) / "repo.git"
        identities = self.identities(op.params)
        async with _WorkDir(self, f"reauthor-op-{op.id}") as work:
            mirror = work / "repo.git"
            await asyncio.to_thread(mirror_clone, self.s.git, str(source), mirror, auth=False, local=True)
            await progress("✍️ Rewriting authors locally and verifying file trees…")
            result = await asyncio.to_thread(
                rewrite_authors, self.s.git, mirror, identities, op.params["new_name"], op.params["new_email"]
            )
            manifest = json.loads((source.parent / "manifest.json").read_text())
            for update in result.updates:
                if manifest["refs"].get(update.ref) != update.old:
                    raise SafetyError(f"Ref {update.ref} does not match the backup. Nothing was pushed.")
            await progress(f"🚀 Atomic force-with-lease push of {len(result.updates)} ref(s)…")
            await asyncio.to_thread(
                self.s.git.run, build_push_args(self.s.backups.url_for(repo.full_name), result.updates),
                cwd=mirror, auth=True,
            )
        return {"commits_reauthored": result.removed, "commits_rewritten": result.rewritten,
                "updated_refs": len(result.updates), "new_identity": f"{op.params['new_name']} <{op.params['new_email']}>",
                "expected_refs": result.expected_refs, "backup_id": op.backup_id}

    async def verify(self, op: Operation, repo: RepoRef, result: dict[str, Any]) -> list[str]:
        remote = important(await asyncio.to_thread(ls_remote, self.s.git, self.s.backups.url_for(repo.full_name)))
        if remote != result["expected_refs"]:
            return [f"GitHub refs differ from the verified local rewrite ({len(set(remote.items()) ^ set(result['expected_refs'].items()))} differences)"]
        return []


# ------------------------------------------------------------- pull requests
class OpenPullRequest(OperationSpec):
    kind = "open_pr"
    verb = "PR"
    label = "pull request"
    requires_backup = False  # a new branch and PR change nothing that exists
    confirm_mode = "button"

    async def analyze(self, repo: RepoRef, params: dict[str, Any], progress: Progress) -> Impact:
        service = self.s.pulls
        await progress("🔍 Reading repository and branch protection…")
        analysis = await service.analyze(repo)
        blockers = list(analysis.blockers)
        capacity = await service.capacity_blocker()
        if capacity:
            blockers.append(capacity)
        change = None
        if not blockers:
            try:
                change = await service.build_change(repo, params["change_kind"], params)
            except ValidationError as exc:
                blockers.append(str(exc))
        settings = await service.settings()
        auto_merge = bool(params.get("auto_merge", settings["pr_auto_merge"]))
        added, removed = change.stats if change else (0, 0)
        lines = [
            f"Repository: {repo} (public)",
            f"Base branch: {analysis.default_branch}" + (" 🛡 protected" if analysis.protection else ""),
            f"Change type: {params['change_kind']}",
            f"File: {change.path if change else '—'}" + (" (new file)" if change and change.is_new else ""),
            f"Diff: +{added} −{removed} line(s)",
            f"Commit: {change.commit_message if change else '—'}",
            f"Open pull requests in this repository: {analysis.open_pulls}",
            f"Auto-merge: {'ON' if auto_merge else 'OFF'} · method: {settings['pr_merge_method']}"
            + (" · delete branch after merge" if settings["pr_delete_branch"] else ""),
        ]
        if analysis.required_checks:
            lines.append("Required checks: " + ", ".join(analysis.required_checks[:6]))
        warnings = list(analysis.notes)
        if auto_merge:
            warnings.append("Auto-merge is ON: this pull request will be merged automatically once every "
                            "required check and review passes, without asking again.")
        warnings.append("Branch protection and required reviews stay enforced by GitHub; the bot never bypasses them.")
        return Impact(f"Open pull request in {repo}", lines, warnings=warnings, blockers=blockers,
                      data={"change": {"path": change.path, "diff": change.diff()} if change else {},
                            "base": analysis.default_branch, "auto_merge": auto_merge})

    async def preflight(self, op: Operation, repo: RepoRef) -> None:
        capacity = await self.s.pulls.capacity_blocker()
        if capacity:
            raise SafetyError(capacity)

    async def execute(self, op: Operation, repo: RepoRef, progress: Progress) -> dict[str, Any]:
        service = self.s.pulls
        analysis = await service.analyze(repo)
        if analysis.blockers:
            raise SafetyError("; ".join(analysis.blockers))
        change = await service.build_change(repo, op.params["change_kind"], op.params)
        settings = await service.settings()
        result = await service.open_pull_request(
            repo, change, analysis,
            branch=op.params.get("branch") or service.branch_name(change.kind),
            auto_merge=bool(op.params.get("auto_merge", settings["pr_auto_merge"])),
            merge_method=str(settings["pr_merge_method"]),
            delete_branch=bool(settings["pr_delete_branch"]),
            operation_id=op.id, progress=progress,
        )
        return result

    async def verify(self, op: Operation, repo: RepoRef, result: dict[str, Any]) -> list[str]:
        pull = await self.s.gh.get_pull(repo.full_name, int(result["number"]))
        problems = []
        if pull["state"] != "open":
            problems.append(f"pull request is {pull['state']}")
        if pull["head"]["ref"] != result["branch"]:
            problems.append("pull request head branch does not match")
        return problems


class MergePullRequest(OperationSpec):
    kind = "merge_pr"
    verb = "MERGE"
    label = "pull request merge"
    requires_backup = False  # merging adds a commit; nothing is rewritten or deleted

    async def _record(self, params: dict[str, Any]):
        record = await self.s.db.get_pull_request(int(params["record_id"]))
        if record is None or record.number is None:
            raise SafetyError("Pull request record not found.")
        return record

    async def analyze(self, repo: RepoRef, params: dict[str, Any], progress: Progress) -> Impact:
        record = await self._record(params)
        settings = await self.s.pulls.settings()
        await progress("🔍 Checking merge state, checks and reviews…")
        status = await self.s.pulls.status(repo, record.number)
        problems = status.blockers(bool(settings["pr_require_checks"]))
        lines = [
            f"Pull request: #{status.number} — {record.title}",
            f"Repository: {repo} · base {record.base} · head {record.branch}",
            f"State: {status.state}{' (draft)' if status.draft else ''} · mergeable: {status.mergeable}"
            f" ({status.mergeable_state})",
            f"Checks: {len(status.checks.passed)} passed · {len(status.checks.failed)} failed · "
            f"{len(status.checks.pending)} pending",
            f"Reviews: {status.approvals} approval(s)"
            + (f" of {status.required_reviews} required" if status.required_reviews else "")
            + (f" · {status.changes_requested} requested changes" if status.changes_requested else ""),
            f"Merge method: {record.merge_method}"
            + (" · branch deleted afterwards" if record.delete_branch else ""),
        ]
        warnings = []
        if status.checks.unreadable:
            warnings.append("Check results are not readable with this token (needs Checks: read and "
                            "Commit statuses: read); required checks are treated as pending.")
        if not settings["pr_require_checks"]:
            warnings.append("'Require successful checks' is OFF, so only GitHub's own rules apply.")
        return Impact(f"Merge pull request #{status.number} in {repo}", lines, warnings=warnings,
                      blockers=problems, data={"number": status.number, "head_sha": status.head_sha})

    async def preflight(self, op: Operation, repo: RepoRef) -> None:
        record = await self._record(op.params)
        settings = await self.s.pulls.settings()
        status = await self.s.pulls.status(repo, record.number)
        problems = status.blockers(bool(settings["pr_require_checks"]))
        if problems:
            raise SafetyError("Cannot merge: " + "; ".join(problems))
        if status.head_sha != (op.impact or {}).get("data", {}).get("head_sha"):
            raise SafetyError("New commits were pushed since the analysis. Check the pull request again.")

    async def execute(self, op: Operation, repo: RepoRef, progress: Progress) -> dict[str, Any]:
        record = await self._record(op.params)
        status = await self.s.pulls.status(repo, record.number)
        await progress(f"🔀 Merging #{record.number} ({record.merge_method})…")
        try:
            return await self.s.pulls.merge(repo, record, status)
        except ValidationError as exc:
            raise SafetyError(str(exc)) from None

    async def verify(self, op: Operation, repo: RepoRef, result: dict[str, Any]) -> list[str]:
        record = await self._record(op.params)
        pull = await self.s.gh.get_pull(repo.full_name, record.number)
        return [] if pull.get("merged") else ["GitHub does not report the pull request as merged"]


# ------------------------------------------------------------ manual protection
class UnlockRepository(OperationSpec):
    kind = "unlock_repo"
    verb = "UNLOCK"
    label = "remove manual protection"
    requires_backup = False
    confirm_mode = "button"
    github_write = False  # only changes the bot's own database

    async def analyze(self, repo: RepoRef, params: dict[str, Any], progress: Progress) -> Impact:
        protection = await self.s.db.get_protection(repo.full_name)
        if protection is None:
            return Impact(f"Unlock {repo}", [], blockers=[f"{repo} is not manually protected."])
        return Impact(
            f"Unlock {repo}",
            [f"Protected since: {protection['created_at']}", f"Reason: {protection.get('reason') or '—'}"],
            warnings=["Destructive and write operations will be possible again (still with the full safety flow)."],
        )

    async def execute(self, op: Operation, repo: RepoRef, progress: Progress) -> dict[str, Any]:
        await self.s.db.unprotect_repo(repo.full_name)
        return {"unlocked": repo.full_name}

    async def verify(self, op: Operation, repo: RepoRef, result: dict[str, Any]) -> list[str]:
        return [] if await self.s.db.get_protection(repo.full_name) is None else ["protection still present"]


ALL_SPECS: list[type[OperationSpec]] = [
    DeleteRepository, RewriteHistory, RestoreBackup, RenameRepository,
    MakePrivate, MakePublic, ArchiveRepository, UnarchiveRepository, ImportRepository,
    CreateRepository, SetDescription, SetHomepage, SetTopics,
    RerunWorkflow, RerunFailedJobs, CancelWorkflowRun, UnlockRepository, ReauthorRepository,
    OpenPullRequest, MergePullRequest,
]
