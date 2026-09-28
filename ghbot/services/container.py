"""Dependency container shared by handlers, with one resource set per GitHub account."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ghbot.config import Account, Settings
from ghbot.db import Database
from ghbot.git.runner import GitRunner
from ghbot.github.api import GitHubAPI
from ghbot.github.client import GitHubClient
from ghbot.services.backups import BackupService

if TYPE_CHECKING:
    from ghbot.services.batch import BatchHistory
    from ghbot.services.bulk import BulkBackups
    from ghbot.services.policy import RepoPolicy
    from ghbot.services.safety import SafetyEngine

ACTIVE_ACCOUNT_SETTING = "active_account"


@dataclass
class AccountResources:
    """Everything bound to one GitHub account (its own token, client and git runner)."""

    username: str
    gh: GitHubAPI
    git: GitRunner
    backups: BackupService
    policy: "RepoPolicy"


@dataclass
class Services:
    settings: Settings
    db: Database
    resources: dict[str, AccountResources] = field(default_factory=dict)
    active: str = ""
    safety: "SafetyEngine" = field(init=False)
    bulk: "BulkBackups" = field(init=False)
    batch: "BatchHistory" = field(init=False)
    lock: "PasswordLock" = field(init=False)  # noqa: F821
    pulls: "PullRequestService" = field(init=False)  # noqa: F821
    review: "ReviewService" = field(init=False)  # noqa: F821

    # ------------------------------------------------------------- accounts
    @property
    def username(self) -> str:
        return self.active

    @property
    def current(self) -> AccountResources:
        return self.resources[self.active]

    @property
    def gh(self) -> GitHubAPI:
        return self.current.gh

    @property
    def git(self) -> GitRunner:
        return self.current.git

    @property
    def backups(self) -> BackupService:
        return self.current.backups

    @property
    def policy(self) -> "RepoPolicy":
        return self.current.policy

    @property
    def account(self) -> Account:
        found = self.settings.account(self.active)
        assert found is not None
        return found

    @property
    def usernames(self) -> list[str]:
        return list(self.resources)

    def setting_key(self, key: str) -> str:
        """Per-account settings key (global settings keep their plain name)."""
        return f"{key}:{self.active.lower()}"

    async def load_active(self) -> None:
        stored = await self.db.get_setting(ACTIVE_ACCOUNT_SETTING)
        if stored and stored in self.resources:
            self.active = stored

    async def switch(self, username: str) -> str:
        match = next((u for u in self.resources if u.lower() == username.lower()), None)
        if match is None:
            raise KeyError(username)
        self.active = match
        await self.db.set_setting(ACTIVE_ACCOUNT_SETTING, match)
        return match

    # -------------------------------------------------------------- wiring
    @classmethod
    def build(cls, settings: Settings) -> Services:
        from ghbot.services.auth import PasswordLock
        from ghbot.services.batch import BatchHistory
        from ghbot.services.bulk import BulkBackups
        from ghbot.services.operations import ALL_SPECS
        from ghbot.services.pullrequests import PullRequestService
        from ghbot.services.review import ReviewService
        from ghbot.services.safety import SafetyEngine

        db = Database(settings.database_path)
        services = cls(settings, db)
        for account in settings.accounts:
            services.resources[account.username] = cls._resources(settings, db, account)
        services.active = settings.accounts[0].username
        services.safety = SafetyEngine(services, ALL_SPECS)
        services.bulk = BulkBackups(services)
        services.batch = BatchHistory(services)
        services.lock = PasswordLock(services)
        services.pulls = PullRequestService(services)
        services.review = ReviewService(services)
        return services

    @staticmethod
    def _resources(settings: Settings, db: Database, account: Account) -> AccountResources:
        from ghbot.services.policy import RepoPolicy

        gh = GitHubAPI(GitHubClient(account.token, settings.github_api_url), account.username)
        git = GitRunner(token=account.token, home=settings.work_path / ".home",
                        timeout=settings.git_timeout)
        backups = BackupService(settings, db, gh, git)
        policy = RepoPolicy(settings, gh, db, account.username)
        backups.policy = policy
        return AccountResources(account.username, gh, git, backups, policy)

    async def close(self) -> None:
        for resources in self.resources.values():
            await resources.gh.client.close()
        self.db.close()
