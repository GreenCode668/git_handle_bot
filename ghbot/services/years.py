"""Commit-year spans per repository (read-only).

Used by /years to show which repositories produce the year tabs next to the
contribution graph on a GitHub profile. It reads the default branch only, with
two API requests per repository (newest commit, then the last page for the
oldest one), so it stays cheap for accounts with many repositories.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ghbot.github.client import GitHubError
from ghbot.logging_setup import get_redactor

if TYPE_CHECKING:
    from ghbot.github.api import GitHubAPI

MAX_CONCURRENCY = 5


@dataclass
class YearSpan:
    repo: str
    first: int | None = None
    last: int | None = None
    commits: int = 0
    error: str = ""

    @property
    def years(self) -> range:
        if self.first is None or self.last is None:
            return range(0)
        return range(self.first, self.last + 1)


def _year(commit: dict[str, Any]) -> int:
    return int(commit["commit"]["committer"]["date"][:4])


async def repo_year_span(gh: GitHubAPI, full_name: str) -> YearSpan:
    try:
        newest = await gh.list_commits(full_name, page=1, per_page=1)
        if not newest.items:
            return YearSpan(full_name)
        total = newest.last_page or 1
        oldest = newest if total == 1 else await gh.list_commits(full_name, page=total, per_page=1)
        items = oldest.items or newest.items
        return YearSpan(full_name, _year(items[-1]), _year(newest.items[0]), total)
    except GitHubError as exc:
        if exc.status == 409:  # empty repository
            return YearSpan(full_name)
        return YearSpan(full_name, error=get_redactor()(str(exc))[:120])


async def collect_year_spans(gh: GitHubAPI, repos: list[str], progress=None) -> list[YearSpan]:
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    done = 0

    async def one(full_name: str) -> YearSpan:
        nonlocal done
        async with semaphore:
            span = await repo_year_span(gh, full_name)
        done += 1
        if progress and done % 5 == 0:
            await progress(f"📅 Reading commit years… {done}/{len(repos)}")
        return span

    spans = await asyncio.gather(*(one(name) for name in repos))
    return sorted(spans, key=lambda s: (s.first is None, s.first or 0, s.repo))


def year_report(spans: list[YearSpan], created_year: int) -> dict[str, Any]:
    """Group repositories by the years they cover; flag years before the account existed."""
    by_year: dict[int, list[str]] = {}
    for span in spans:
        for year in span.years:
            by_year.setdefault(year, []).append(span.repo)
    old_years = sorted((y for y in by_year if y < created_year), reverse=True)
    culprits: dict[str, list[int]] = {}
    for year in old_years:
        for repo in by_year[year]:
            culprits.setdefault(repo, []).append(year)
    return {
        "by_year": dict(sorted(by_year.items(), reverse=True)),
        "old_years": old_years,
        "culprits": dict(sorted(culprits.items(), key=lambda kv: min(kv[1]))),
        "empty": [s.repo for s in spans if s.first is None and not s.error],
        "errors": [(s.repo, s.error) for s in spans if s.error],
        "oldest": min((s.first for s in spans if s.first), default=None),
    }
