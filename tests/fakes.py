"""In-memory GitHub fake and a Services factory wired to local git remotes."""

from __future__ import annotations

import itertools
import shutil
from pathlib import Path
from typing import Any

from ghbot.config import Account, Settings
from ghbot.db import Database
from ghbot.git.runner import GitRunner
from ghbot.github.client import GitHubError, GitHubNotFound, Page
from ghbot.services.backups import BackupService
from ghbot.services.container import AccountResources, Services
from ghbot.services.auth import PasswordLock
from ghbot.services.batch import BatchHistory
from ghbot.services.bulk import BulkBackups
from ghbot.services.operations import ALL_SPECS
from ghbot.services.policy import RepoPolicy
from ghbot.services.pullrequests import PullRequestService
from ghbot.services.review import ReviewService
from ghbot.services.safety import SafetyEngine
from tests.gitutil import git

_ids = itertools.count(1000)


class FakeClient:
    last_scopes = "repo, workflow, delete_repo, user"

    async def close(self) -> None:
        pass


class FakeGitHub:
    def __init__(self, username: str, remotes: Path) -> None:
        self.username = username
        self.remotes = remotes
        self.client = FakeClient()
        self.repos: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, Any]] = []
        self.runs = {}
        self.files: dict[tuple[str, str], str] = {}
        self.branches: dict[tuple[str, str], str] = {}
        self.protection: dict[tuple[str, str], dict[str, Any]] = {}
        self.pulls: dict[tuple[str, int], dict[str, Any]] = {}
        self.reviews: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self.checks: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.statuses: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.pull_counter = 0
        self.annotations: dict[int, list[dict[str, Any]]] = {}
        self.workflow_runs: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.jobs: dict[int, list[dict[str, Any]]] = {}
        self.job_log: dict[int, str] = {}

    def add_repo(self, name: str, **fields: Any) -> dict[str, Any]:
        meta = {
            "id": next(_ids), "name": name, "full_name": f"{self.username}/{name}", "private": False,
            "visibility": "public", "owner": {"login": self.username}, "archived": False, "default_branch": "main", "size": 10,
            "stargazers_count": 0, "forks_count": 0, "watchers_count": 0, "open_issues_count": 0,
            "has_wiki": False, "topics": [], "description": None, "homepage": None, **fields,
        }
        self.repos[name.lower()] = meta
        return meta

    async def get_repo(self, full_name: str) -> dict[str, Any]:
        name = full_name.split("/", 1)[1].lower()
        if name not in self.repos:
            raise GitHubNotFound(404, "Not Found")
        return dict(self.repos[name])

    async def repo_exists(self, full_name: str) -> bool:
        return full_name.split("/", 1)[1].lower() in self.repos

    async def delete_repo(self, full_name: str) -> None:
        name = full_name.split("/", 1)[1]
        self.calls.append(("delete", full_name))
        self.repos.pop(name.lower())
        shutil.rmtree(self.remotes / f"{name}.git")

    async def create_repo(self, name: str, *, private: bool, **kw: Any) -> dict[str, Any]:
        self.calls.append(("create", name))
        git(self.remotes, "init", "-q", "--bare", f"{name}.git")
        return self.add_repo(name, private=private)

    async def update_repo(self, full_name: str, **fields: Any) -> dict[str, Any]:
        self.calls.append(("update", fields))
        meta = self.repos[full_name.split("/", 1)[1].lower()]
        if "name" in fields:
            self.repos.pop(meta["name"].lower())
            meta["name"] = fields["name"]
            meta["full_name"] = f"{self.username}/{fields['name']}"
            self.repos[fields["name"].lower()] = meta
        if "private" in fields:
            meta["visibility"] = "private" if fields["private"] else "public"
        meta.update({k: v for k, v in fields.items() if k != "name"})
        return dict(meta)

    async def get_file(self, full_name: str, path: str):
        key = (full_name.lower(), path)
        if key not in self.files:
            return None
        content = self.files[key]
        return content, f"sha-{abs(hash(content)) % 10**8}"

    async def put_file(self, full_name: str, path: str, content: str, message: str, sha: str | None):
        key = (full_name.lower(), path)
        current = self.files.get(key)
        expected = f"sha-{abs(hash(current)) % 10**8}" if current is not None else None
        if sha != expected:  # optimistic concurrency, like the contents API
            raise GitHubError(409, "README changed meanwhile")
        self.files[key] = content
        self.calls.append(("put_file", full_name, path))
        return {"content": {"sha": f"sha-{abs(hash(content)) % 10**8}"}}

    async def list_social_accounts(self) -> list[dict[str, Any]]:
        return [{"url": "https://discord.com/users/1"}]

    async def get_user(self) -> dict[str, Any]:
        return {"login": self.username, "name": "Colin Trator", "bio": "Software engineer",
                "company": "Canva", "location": "Vienna, Austria", "blog": "https://example.com",
                "public_repos": len([r for r in self.repos.values() if not r["private"]]),
                "followers": 0, "following": 0, "created_at": "2023-04-07T00:00:00Z",
                "html_url": f"https://github.com/{self.username}"}

    async def replace_topics(self, full_name: str, topics: list[str]) -> None:
        self.calls.append(("topics", topics))
        self.repos[full_name.split("/", 1)[1].lower()]["topics"] = list(topics)

    async def count_open_pulls(self, full_name: str) -> int:
        return 0

    async def all_repos(self, *, public_only: bool = True, max_items: int = 1000) -> list[dict[str, Any]]:
        repos = [dict(r) for r in self.repos.values()]
        return [r for r in repos if not r["private"]] if public_only else repos

    async def compare(self, full_name: str, base: str, head: str) -> dict[str, Any]:
        return {"status": "ahead", "ahead_by": 1, "behind_by": 0}

    runs: dict[int, dict[str, Any]] = {}

    async def get_run(self, full_name: str, run_id: int) -> dict[str, Any]:
        if run_id not in self.runs:
            raise GitHubNotFound(404, "Not Found")
        return dict(self.runs[run_id])

    async def rerun(self, full_name: str, run_id: int) -> None:
        self.calls.append(("rerun", run_id))
        self.runs[run_id].update(status="queued", conclusion=None, run_attempt=self.runs[run_id]["run_attempt"] + 1)

    async def rerun_failed(self, full_name: str, run_id: int) -> None:
        await self.rerun(full_name, run_id)

    async def cancel_run(self, full_name: str, run_id: int) -> None:
        self.calls.append(("cancel", run_id))
        self.runs[run_id].update(status="completed", conclusion="cancelled")

    async def list_branches(self, full_name: str, page: int = 1, per_page: int = 30, protected=None) -> Page:
        return Page([], 1, False, 1)

    # ---------------------------------------------------------- pull requests
    def add_branch(self, full_name: str, branch: str, sha: str = "basesha1") -> None:
        self.branches[(full_name.lower(), branch)] = sha

    def protect(self, full_name: str, branch: str, *, checks: list[str] | None = None, reviews: int = 0) -> None:
        self.protection[(full_name.lower(), branch)] = {
            "required_status_checks": {"contexts": checks or []},
            "required_pull_request_reviews": {"required_approving_review_count": reviews},
        }

    def set_checks(self, full_name: str, sha: str, runs: list[dict[str, Any]]) -> None:
        self.checks[(full_name.lower(), sha)] = runs

    def approve(self, full_name: str, number: int, user: str = "reviewer", state: str = "APPROVED") -> None:
        self.reviews.setdefault((full_name.lower(), number), []).append(
            {"user": {"login": user}, "state": state})

    async def get_branch(self, full_name: str, branch: str) -> dict[str, Any]:
        sha = self.branches.get((full_name.lower(), branch))
        if sha is None:
            raise GitHubNotFound(404, "Branch not found")
        return {"name": branch, "commit": {"sha": sha}}

    async def branch_protection(self, full_name: str, branch: str) -> dict[str, Any] | None:
        return self.protection.get((full_name.lower(), branch))

    async def create_ref(self, full_name: str, ref: str, sha: str) -> dict[str, Any]:
        branch = ref.removeprefix("refs/heads/")
        key = (full_name.lower(), branch)
        if key in self.branches:
            raise GitHubError(422, "Reference already exists")
        self.branches[key] = sha
        self.calls.append(("create_ref", ref))
        return {"ref": ref, "object": {"sha": sha}}

    async def delete_ref(self, full_name: str, ref: str) -> None:
        branch = ref.removeprefix("heads/")
        self.branches.pop((full_name.lower(), branch), None)
        self.calls.append(("delete_ref", branch))

    async def put_file_on_branch(self, full_name: str, path: str, content: str, message: str,
                                 sha: str | None, branch: str) -> dict[str, Any]:
        self.files[(full_name.lower(), path)] = content
        commit_sha = f"commit-{abs(hash(content)) % 10**6}"
        self.branches[(full_name.lower(), branch)] = commit_sha
        self.calls.append(("commit", branch, path))
        return {"commit": {"sha": commit_sha}, "content": {"sha": f"blob-{abs(hash(content)) % 10**6}"}}

    async def create_pull(self, full_name: str, *, title: str, head: str, base: str, body: str,
                          draft: bool = False) -> dict[str, Any]:
        self.pull_counter += 1
        number = self.pull_counter
        pull = {
            "number": number, "title": title, "state": "open", "draft": bool(draft), "merged": False,
            "mergeable": True, "mergeable_state": "clean",
            # a branch pushed with git (not via create_ref) is not in self.branches
            "head": {"ref": head, "sha": self.branches.get((full_name.lower(), head), f"pushed-{number}")},
            "base": {"ref": base},
            "html_url": f"https://github.com/{full_name}/pull/{number}",
        }
        self.pulls[(full_name.lower(), number)] = pull
        self.calls.append(("create_pull", full_name, number))
        return dict(pull)

    async def get_pull(self, full_name: str, number: int) -> dict[str, Any]:
        pull = self.pulls.get((full_name.lower(), int(number)))
        if pull is None:
            raise GitHubNotFound(404, "Not Found")
        return dict(pull)

    async def list_reviews(self, full_name: str, number: int) -> list[dict[str, Any]]:
        return list(self.reviews.get((full_name.lower(), int(number)), []))

    async def merge_pull(self, full_name: str, number: int, *, method: str, title=None) -> dict[str, Any]:
        pull = self.pulls[(full_name.lower(), int(number))]
        if pull["mergeable"] is not True or pull["mergeable_state"] in ("dirty", "blocked"):
            raise GitHubError(405, "Pull Request is not mergeable")  # GitHub refuses, as it would live
        pull.update(merged=True, state="closed")
        self.calls.append(("merge", full_name, number, method))
        return {"merged": True, "sha": f"merge-{number}"}

    async def check_run_annotations(self, full_name: str, check_run_id: int) -> list[dict[str, Any]]:
        return list(self.annotations.get(int(check_run_id), []))

    async def runs_for_sha(self, full_name: str, sha: str, per_page: int = 5) -> list[dict[str, Any]]:
        return list(self.workflow_runs.get((full_name.lower(), sha), []))

    async def run_jobs(self, full_name: str, run_id: int) -> list[dict[str, Any]]:
        return list(self.jobs.get(int(run_id), []))

    async def job_logs(self, full_name: str, job_id: int) -> str:
        return self.job_log.get(int(job_id), "")

    async def check_runs(self, full_name: str, ref: str) -> dict[str, Any]:
        runs = self.checks.get((full_name.lower(), ref), [])
        return {"total_count": len(runs), "check_runs": runs}

    async def combined_status(self, full_name: str, ref: str) -> dict[str, Any]:
        statuses = self.statuses.get((full_name.lower(), ref), [])
        return {"state": "success" if not statuses else statuses[0]["state"], "statuses": statuses}

    async def search_count(self, query: str) -> int:
        return 7


def make_services(tmp_path: Path, username: str = "me", extra: list[str] | None = None):
    """Services wired to local git remotes, with one FakeGitHub per configured account."""
    remotes = tmp_path / "remotes"
    remotes.mkdir(exist_ok=True)
    settings = Settings(
        telegram_bot_token="123456:" + "x" * 35,
        telegram_allowed_user_id=1,
        github_token="ghp_" + "t" * 36,
        github_username=username,
        backup_path=(tmp_path / "backups").resolve(),
        database_path=tmp_path / "bot.db",
        work_path=(tmp_path / "botwork").resolve(),
        extra_accounts=tuple(Account(name, "ghp_" + "e" * 36) for name in (extra or [])),
    )
    settings.backup_path.mkdir()
    settings.work_path.mkdir()
    db = Database(settings.database_path)
    services = Services(settings, db)
    runner = GitRunner(token=None, home=tmp_path / "home", allowed_protocols="https:file")

    def url_for(full_name: str) -> str:
        return str(remotes / f"{full_name.split('/', 1)[1]}.git")

    first: FakeGitHub | None = None
    for account in settings.accounts:
        account_dir = remotes if account.username == username else remotes / account.username
        account_dir.mkdir(exist_ok=True)
        gh = FakeGitHub(account.username, account_dir)
        backups = BackupService(settings, db, gh, runner, url_for=url_for)  # type: ignore[arg-type]
        policy = RepoPolicy(settings, gh, db, account.username)  # type: ignore[arg-type]
        backups.policy = policy
        services.resources[account.username] = AccountResources(account.username, gh, runner, backups, policy)  # type: ignore[arg-type]
        first = first or gh
    services.active = username
    services.safety = SafetyEngine(services, ALL_SPECS)
    services.bulk = BulkBackups(services)
    services.batch = BatchHistory(services)
    services.lock = PasswordLock(services)
    services.pulls = PullRequestService(services)
    services.review = ReviewService(services)
    return services, first, remotes
