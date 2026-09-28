"""Central repository policy.

The bot manages ONLY repositories that are
  1. owned by GITHUB_USERNAME, and
  2. public.

Every GitHub write goes through `RepoPolicy.ensure_writable` (enforced by the
SafetyEngine for all operations, and by the few direct writers such as backups),
using *fresh* repository metadata, so a repository that became private since it
was listed can never be modified. Private repositories are hidden from read-only
views unless SHOW_PRIVATE_REPOS=true is configured explicitly.
"""

from __future__ import annotations

from typing import Any

from ghbot.config import Settings
from ghbot.db import Database
from ghbot.github.api import GitHubAPI
from ghbot.github.client import GitHubNotFound
from ghbot.validators import RepoRef


class PolicyError(Exception):
    """A repository is outside the managed scope. The message is safe to show."""


def is_owned(meta: dict[str, Any], username: str) -> bool:
    owner = (meta.get("owner") or {}).get("login", "")
    return owner.lower() == username.lower()


def is_public(meta: dict[str, Any]) -> bool:
    return meta.get("private") is False and meta.get("visibility", "public") == "public"


class RepoPolicy:
    def __init__(self, settings: Settings, gh: GitHubAPI, db: Database, username: str | None = None) -> None:
        self.settings = settings
        self.gh = gh
        self.db = db
        self.username = username or settings.github_username

    @property
    def show_private(self) -> bool:
        return self.settings.show_private_repos

    # ------------------------------------------------------------- read-only
    def is_visible(self, meta: dict[str, Any]) -> bool:
        return is_owned(meta, self.username) and (is_public(meta) or self.show_private)

    def filter_visible(self, repos: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [r for r in repos if self.is_visible(r)]

    async def readable(self, repo: RepoRef) -> dict[str, Any]:
        try:
            meta = await self.gh.get_repo(repo.full_name)
        except GitHubNotFound:
            meta = None
        if meta is None or not self.is_visible(meta):
            # Same message for missing and hidden repositories: existence is not revealed.
            raise PolicyError(f"Repository {repo.full_name} was not found or is not managed by this bot.")
        return meta

    # ----------------------------------------------------------------- write
    def check_writable_meta(self, meta: dict[str, Any]) -> None:
        name = meta.get("full_name", "?")
        if not is_owned(meta, self.username):
            raise PolicyError(f"🛑 {name} is not owned by {self.username}; the bot will not modify it.")
        if not is_public(meta):
            raise PolicyError(
                f"🛑 {name} is {meta.get('visibility', 'private')}. Only PUBLIC repositories can be modified by this bot."
            )

    async def ensure_not_protected(self, repo: RepoRef) -> None:
        protection = await self.db.get_protection(repo.full_name)
        if protection:
            raise PolicyError(f"🔒 {repo.full_name} is manually protected. Use /unlock {repo.name} first.")

    async def ensure_writable(self, repo: RepoRef) -> dict[str, Any]:
        """Fresh metadata check for an existing repository that is about to be modified."""
        await self.ensure_not_protected(repo)
        try:
            meta = await self.gh.get_repo(repo.full_name)
        except GitHubNotFound:
            raise PolicyError(f"Repository {repo.full_name} was not found.") from None
        self.check_writable_meta(meta)
        return meta

    async def ensure_creatable(self, repo: RepoRef, *, private: bool) -> None:
        """For operations that create a repository: it must be public and the name unused."""
        await self.ensure_not_protected(repo)
        if private:
            raise PolicyError("🛑 This bot only creates PUBLIC repositories.")
        if repo.owner.lower() != self.username.lower():
            raise PolicyError("🛑 Repositories can only be created for the configured account.")

    async def target_state(self, repo: RepoRef) -> dict[str, Any] | None:
        try:
            return await self.gh.get_repo(repo.full_name)
        except GitHubNotFound:
            return None
