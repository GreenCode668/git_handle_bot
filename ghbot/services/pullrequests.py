"""Pull request automation for real maintenance changes.

Everything goes through the existing safety flow: the repository policy (owned +
public + not manually protected), the repository lock, a confirmation, and a
verification after the fact. GitHub's own rules are never bypassed: merges use the
merge API, so branch protection and required reviews are enforced by GitHub, and
the bot refuses to merge while a required check is missing or failing.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ghbot.github.client import GitHubError, GitHubNotFound
from ghbot.validators import ValidationError, validate_branch_name
from ghbot.validators import RepoRef

if TYPE_CHECKING:
    from ghbot.services.container import Services

CHANGE_KINDS = ("readme", "file", "replace", "whitespace")
MERGE_METHODS = ("squash", "merge", "rebase")

PR_DEFAULTS: dict[str, Any] = {
    "pr_auto_merge": False,
    "pr_merge_method": "squash",
    "pr_delete_branch": True,
    "pr_require_checks": True,
    "pr_max_concurrent": 3,
    "pr_allowed_repos": [],  # empty = every managed repository
}

MAX_PATH = 200
_PATH_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._/-]{0,198}[A-Za-z0-9._-])?$")


def validate_path(path: str) -> str:
    # removeprefix, not lstrip: lstrip("./") would turn "../../etc/passwd" into "etc/passwd"
    path = path.strip().removeprefix("./")
    if (not _PATH_RE.fullmatch(path) or ".." in path or "//" in path
            or path.startswith((".git", "/")) or path.endswith("/")):
        raise ValidationError("Invalid file path. Use a repository-relative path such as docs/README.md.")
    return path


def validate_commit_message(text: str) -> str:
    text = text.strip()
    if not 3 <= len(text) <= 200 or "\n" in text:
        raise ValidationError("The commit message must be a single line of 3-200 characters.")
    return text


@dataclass
class Change:
    """One file change ready to be committed."""

    kind: str
    path: str
    new_content: str
    old_content: str | None
    commit_message: str
    title: str
    body: str
    sha: str | None = None  # existing blob sha, None when the file is new

    @property
    def is_new(self) -> bool:
        return self.old_content is None

    def diff(self, max_lines: int = 40) -> str:
        old = (self.old_content or "").splitlines()
        new = self.new_content.splitlines()
        lines = list(difflib.unified_diff(old, new, fromfile=f"a/{self.path}", tofile=f"b/{self.path}", lineterm=""))
        if len(lines) > max_lines:
            lines = lines[:max_lines] + [f"… ({len(lines) - max_lines} more diff lines)"]
        return "\n".join(lines) or "(no textual difference)"

    @property
    def stats(self) -> tuple[int, int]:
        old = (self.old_content or "").splitlines()
        new = self.new_content.splitlines()
        added = sum(1 for line in difflib.unified_diff(old, new, lineterm="") if line.startswith("+") and not line.startswith("+++"))
        removed = sum(1 for line in difflib.unified_diff(old, new, lineterm="") if line.startswith("-") and not line.startswith("---"))
        return added, removed


@dataclass
class RepoAnalysis:
    repo: str
    default_branch: str
    base_sha: str
    protection: dict[str, Any] | None
    required_checks: list[str]
    required_reviews: int
    open_pulls: int
    open_bot_pulls: int
    blockers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class ChecksSummary:
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    unreadable: bool = False  # token lacks Checks/Commit statuses read

    @property
    def total(self) -> int:
        return len(self.passed) + len(self.failed) + len(self.pending)

    @property
    def all_green(self) -> bool:
        return not self.failed and not self.pending


@dataclass
class PullStatus:
    number: int
    state: str
    draft: bool
    merged: bool
    mergeable: bool | None
    mergeable_state: str
    head_sha: str
    checks: ChecksSummary
    approvals: int
    changes_requested: int
    required_reviews: int
    required_checks: list[str]
    html_url: str

    def blockers(self, require_checks: bool) -> list[str]:
        problems: list[str] = []
        if self.merged:
            problems.append("already merged")
        if self.state != "open":
            problems.append(f"pull request is {self.state}")
        if self.draft:
            problems.append("pull request is a draft")
        if self.mergeable is False or self.mergeable_state == "dirty":
            problems.append("merge conflict with the base branch")
        if self.mergeable is None:
            problems.append("GitHub is still calculating mergeability")
        if self.changes_requested:
            problems.append(f"{self.changes_requested} review(s) requested changes")
        if self.required_reviews and self.approvals < self.required_reviews:
            problems.append(f"{self.approvals}/{self.required_reviews} required approvals")
        if require_checks:
            if self.checks.failed:
                problems.append("failing checks: " + ", ".join(self.checks.failed[:5]))
            if self.checks.pending:
                problems.append("checks still running: " + ", ".join(self.checks.pending[:5]))
        if self.mergeable_state == "blocked" and not problems:
            problems.append("GitHub reports the branch as blocked (protection rule not satisfied)")
        return problems


class PullRequestService:
    def __init__(self, services: Services) -> None:
        self.s = services

    # ------------------------------------------------------------- settings
    async def settings(self) -> dict[str, Any]:
        values = {}
        for key, default in PR_DEFAULTS.items():
            stored = await self.s.db.get_setting(self.s.setting_key(key))
            values[key] = default if stored is None else stored
        return values

    async def set_setting(self, key: str, value: Any) -> None:
        if key not in PR_DEFAULTS:
            raise ValidationError("Unknown pull request setting.")
        await self.s.db.set_setting(self.s.setting_key(key), value)

    async def repo_allowed(self, repo: RepoRef) -> bool:
        allowed = (await self.settings())["pr_allowed_repos"]
        return not allowed or repo.full_name.lower() in {a.lower() for a in allowed}

    # ------------------------------------------------------------- analysis
    async def analyze(self, repo: RepoRef) -> RepoAnalysis:
        meta = await self.s.policy.readable(repo)
        blockers: list[str] = []
        notes: list[str] = []
        try:
            self.s.policy.check_writable_meta(meta)
        except Exception as exc:  # noqa: BLE001 - PolicyError message is safe to show
            blockers.append(str(exc))
        if meta.get("archived"):
            blockers.append("Repository is archived (read-only).")
        if not await self.repo_allowed(repo):
            blockers.append("Repository is not in the allowed list (/pr_settings).")

        default_branch = meta.get("default_branch") or "main"
        base_sha = ""
        try:
            branch = await self.s.gh.get_branch(repo.full_name, default_branch)
            base_sha = branch["commit"]["sha"]
        except GitHubError as exc:
            blockers.append(f"Cannot read {default_branch}: {exc}")

        protection = await self.s.gh.branch_protection(repo.full_name, default_branch)
        required_checks: list[str] = []
        required_reviews = 0
        if protection:
            checks = (protection.get("required_status_checks") or {})
            required_checks = list(checks.get("contexts") or [c["context"] for c in checks.get("checks", [])])
            reviews = protection.get("required_pull_request_reviews") or {}
            required_reviews = int(reviews.get("required_approving_review_count") or 0)
            notes.append("Base branch is protected: GitHub enforces its rules; the bot never bypasses them.")
            if required_reviews:
                notes.append(f"{required_reviews} approving review(s) required: you must approve the PR yourself.")

        open_pulls = await self.s.gh.count_open_pulls(repo.full_name)
        records, _ = await self.s.db.list_pull_requests(account=self.s.active, repo=repo.full_name,
                                                        statuses=["opening", "open", "merging"])
        return RepoAnalysis(repo.full_name, default_branch, base_sha, protection, required_checks,
                            required_reviews, open_pulls, len(records), blockers, notes)

    async def capacity_blocker(self) -> str | None:
        settings = await self.settings()
        active = await self.s.db.count_active_pull_requests(self.s.active)
        limit = int(settings["pr_max_concurrent"])
        if active >= limit:
            return f"{active} pull request operation(s) are already active (limit {limit}, /pr_settings)."
        return None

    # -------------------------------------------------------------- changes
    async def build_change(self, repo: RepoRef, kind: str, params: dict[str, Any]) -> Change:
        if kind not in CHANGE_KINDS:
            raise ValidationError("Unknown change type.")
        path = validate_path(params.get("path") or ("README.md" if kind == "readme" else ""))
        current = await self.s.gh.get_file(repo.full_name, path)
        old_content, sha = (current[0], current[1]) if current else (None, None)

        if kind == "readme":
            text = (params.get("text") or "").strip()
            if not text:
                raise ValidationError("Send the text to add.")
            base = (old_content or f"# {repo.name}\n")
            new_content = base.rstrip("\n") + "\n\n" + text.strip() + "\n"
            message = validate_commit_message(params.get("message") or f"docs: update {path}")
            title = params.get("title") or f"docs: update {path}"
        elif kind == "file":
            content = params.get("content")
            if content is None or not str(content).strip():
                raise ValidationError("Send the file content.")
            new_content = str(content).rstrip("\n") + "\n"
            message = validate_commit_message(params.get("message") or f"chore: update {path}")
            title = params.get("title") or f"chore: update {path}"
        elif kind == "replace":
            if old_content is None:
                raise ValidationError(f"{path} does not exist in this repository.")
            old_str, new_str = params.get("old_str") or "", params.get("new_str") or ""
            if not old_str:
                raise ValidationError("Send the exact text to replace.")
            occurrences = old_content.count(old_str)
            if occurrences == 0:
                raise ValidationError(f"{old_str[:60]!r} does not appear in {path}.")
            if occurrences > 1:
                raise ValidationError(f"{old_str[:60]!r} appears {occurrences} times in {path}; make it unique.")
            new_content = old_content.replace(old_str, new_str)
            message = validate_commit_message(params.get("message") or f"chore: update {path}")
            title = params.get("title") or message
        else:  # whitespace
            if old_content is None:
                raise ValidationError(f"{path} does not exist in this repository.")
            new_content = "\n".join(line.rstrip() for line in old_content.splitlines()).rstrip("\n") + "\n"
            message = validate_commit_message(params.get("message") or f"style: clean whitespace in {path}")
            title = params.get("title") or message

        if old_content is not None and new_content == old_content:
            raise ValidationError("That change would not modify the file. Nothing to do.")

        body = params.get("body") or (
            f"{message}\n\nOpened from the GitHub Telegram bot. Change type: {kind}."
        )
        return Change(kind, path, new_content, old_content, message, title[:200], body, sha)

    # --------------------------------------------------------------- create
    def branch_name(self, kind: str, now: datetime | None = None) -> str:
        stamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
        return validate_branch_name(f"bot/{kind}-{stamp}")

    async def open_pull_request(self, repo: RepoRef, change: Change, analysis: RepoAnalysis, *,
                                branch: str, auto_merge: bool, merge_method: str, delete_branch: bool,
                                operation_id: int | None = None, progress=None) -> dict[str, Any]:
        record = await self.s.db.create_pull_request(
            account=self.s.active, repo=repo.full_name, number=None, branch=branch,
            base=analysis.default_branch, change_type=change.kind, title=change.title, status="opening",
            auto_merge=int(auto_merge), merge_method=merge_method, delete_branch=int(delete_branch),
            head_sha=None, html_url=None, operation_id=operation_id,
        )
        try:
            if progress:
                await progress(f"🌿 Creating branch {branch}…")
            await self.s.gh.create_ref(repo.full_name, f"refs/heads/{branch}", analysis.base_sha)
            if progress:
                await progress(f"📝 Committing {change.path}…")
            commit = await self.s.gh.put_file_on_branch(repo.full_name, change.path, change.new_content,
                                                        change.commit_message, change.sha, branch)
            head_sha = (commit.get("commit") or {}).get("sha")
            if progress:
                await progress("🔀 Opening the pull request…")
            pull = await self.s.gh.create_pull(repo.full_name, title=change.title, head=branch,
                                               base=analysis.default_branch, body=change.body)
        except GitHubError as exc:
            await self.s.db.update_pull_request(record.id, status="failed", error=str(exc)[:500])
            await self._cleanup_branch(repo, branch)
            raise
        await self.s.db.update_pull_request(record.id, status="open", number=pull["number"],
                                            head_sha=head_sha or pull["head"]["sha"], html_url=pull["html_url"])
        return {"record_id": record.id, "number": pull["number"], "html_url": pull["html_url"],
                "branch": branch, "head_sha": head_sha or pull["head"]["sha"], "path": change.path}

    async def _cleanup_branch(self, repo: RepoRef, branch: str) -> None:
        try:
            await self.s.gh.delete_ref(repo.full_name, f"heads/{branch}")
        except (GitHubError, GitHubNotFound):
            pass

    # --------------------------------------------------------------- status
    async def checks_for(self, repo: RepoRef, sha: str, required: list[str]) -> ChecksSummary:
        """Check runs plus legacy commit statuses for one commit.

        A required check that has not reported yet counts as pending, so auto-merge waits
        instead of merging into a protected branch before its checks arrive.
        """
        summary = ChecksSummary()
        runs = await self.s.gh.check_runs(repo.full_name, sha)
        statuses = await self.s.gh.combined_status(repo.full_name, sha)
        summary.unreadable = bool(runs.get("forbidden")) or bool(statuses.get("forbidden"))

        for run in runs.get("check_runs", []):
            name = run.get("name", "check")
            if run.get("status") != "completed":
                summary.pending.append(name)
            elif run.get("conclusion") in ("success", "neutral", "skipped"):
                summary.passed.append(name)
            else:
                summary.failed.append(name)
        for item in statuses.get("statuses", []):
            name = item.get("context", "status")
            state = item.get("state")
            if state == "pending":
                summary.pending.append(name)
            elif state == "success":
                summary.passed.append(name)
            else:
                summary.failed.append(name)

        reported = set(summary.passed) | set(summary.failed) | set(summary.pending)
        summary.pending.extend(name for name in required if name not in reported)
        return summary

    async def status(self, repo: RepoRef, number: int) -> PullStatus:
        pull = await self.s.gh.get_pull(repo.full_name, number)
        head_sha = pull["head"]["sha"]
        protection = await self.s.gh.branch_protection(repo.full_name, pull["base"]["ref"])
        required_checks: list[str] = []
        required_reviews = 0
        if protection:
            checks = protection.get("required_status_checks") or {}
            required_checks = list(checks.get("contexts") or [c["context"] for c in checks.get("checks", [])])
            required_reviews = int((protection.get("required_pull_request_reviews") or {})
                                   .get("required_approving_review_count") or 0)
        reviews = await self.s.gh.list_reviews(repo.full_name, number)
        latest: dict[str, str] = {}
        for review in reviews:
            user = (review.get("user") or {}).get("login", "?")
            if review.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
                latest[user] = review["state"]
        return PullStatus(
            number=number, state=pull["state"], draft=bool(pull.get("draft")), merged=bool(pull.get("merged")),
            mergeable=pull.get("mergeable"), mergeable_state=pull.get("mergeable_state") or "unknown",
            head_sha=head_sha,
            checks=await self.checks_for(repo, head_sha, required_checks),
            approvals=sum(1 for state in latest.values() if state == "APPROVED"),
            changes_requested=sum(1 for state in latest.values() if state == "CHANGES_REQUESTED"),
            required_reviews=required_reviews, required_checks=required_checks, html_url=pull["html_url"],
        )

    async def merge(self, repo: RepoRef, record, status: PullStatus) -> dict[str, Any]:
        settings = await self.settings()
        problems = status.blockers(bool(settings["pr_require_checks"]))
        if problems:
            raise ValidationError("Cannot merge: " + "; ".join(problems))
        await self.s.db.update_pull_request(record.id, status="merging")
        try:
            result = await self.s.gh.merge_pull(repo.full_name, record.number, method=record.merge_method)
        except GitHubError as exc:
            await self.s.db.update_pull_request(record.id, status="open", error=str(exc)[:500])
            raise
        deleted = False
        if record.delete_branch:
            await self._cleanup_branch(repo, record.branch)
            deleted = True
        await self.s.db.update_pull_request(record.id, status="merged", merged_at=datetime.now(UTC),
                                            head_sha=result.get("sha") or record.head_sha, error=None)
        return {"merged": True, "sha": result.get("sha"), "branch_deleted": deleted,
                "number": record.number, "method": record.merge_method}


    # --------------------------------------------------------- auto-merge job
    async def auto_merge_round(self, notify=None) -> list[str]:
        """Merge pull requests whose auto-merge is on, once GitHub allows it.

        Consent was given when the pull request was confirmed with auto-merge ON.
        Nothing is bypassed: the same blockers as a manual merge apply.
        """
        settings = await self.settings()
        records, _ = await self.s.db.list_pull_requests(account=self.s.active, statuses=["open"], limit=50)
        merged: list[str] = []
        for record in records:
            if not record.auto_merge or record.number is None:
                continue
            repo = RepoRef(*record.repo.split("/", 1))
            try:
                await self.s.policy.ensure_writable(repo)
                status = await self.status(repo, record.number)
                if status.blockers(bool(settings["pr_require_checks"])):
                    continue
                result = await self.merge(repo, record, status)
            except (GitHubError, ValidationError) as exc:
                await self.s.db.update_pull_request(record.id, error=str(exc)[:300])
                continue
            except Exception:  # noqa: BLE001 - policy refusals and the like must not kill the job
                continue
            await self.s.db.log_simple("pr_auto_merge", record.repo, "done",
                                       params={"number": record.number}, result=result)
            merged.append(f"{record.repo}#{record.number}")
            if notify:
                await notify(f"🤖 Auto-merged {record.repo}#{record.number} ({record.merge_method}) "
                             f"after all required checks passed.")
        return merged
