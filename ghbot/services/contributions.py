"""Contributions per year (GraphQL), the source of the year tabs on a GitHub profile.

A year tab exists when GitHub counted contributions for that year. Commit
contributions are attributed to the account that authored them, per repository,
so this tells exactly which repository keeps a given year alive.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ghbot.github.api import GitHubAPI

QUERY = """query($login:String!,$from:DateTime!,$to:DateTime!){
  user(login:$login){
    contributionsCollection(from:$from,to:$to){
      totalCommitContributions
      totalIssueContributions
      totalPullRequestContributions
      restrictedContributionsCount
      hasAnyContributions
      commitContributionsByRepository(maxRepositories:100){
        repository{ nameWithOwner isPrivate owner{login} }
        contributions{ totalCount }
      }
    }
  }
}"""

MAX_CONCURRENCY = 3


@dataclass
class YearContributions:
    year: int
    commits: int = 0
    issues: int = 0
    pull_requests: int = 0
    restricted: int = 0
    has_any: bool = False
    repos: list[tuple[str, int]] = field(default_factory=list)

    @property
    def owned_repos(self) -> list[tuple[str, int]]:
        return self.repos

    @property
    def removable(self) -> bool:
        """True when every counted commit comes from repositories the bot can manage."""
        return self.commits > 0 and sum(count for _, count in self.repos) == self.commits


async def year_contributions(gh: GitHubAPI, login: str, year: int) -> YearContributions:
    data = await gh.client.graphql(QUERY, {"login": login, "from": f"{year}-01-01T00:00:00Z",
                                           "to": f"{year}-12-31T23:59:59Z"})
    collection: dict[str, Any] = data["user"]["contributionsCollection"]
    repos = [(entry["repository"]["nameWithOwner"], entry["contributions"]["totalCount"])
             for entry in collection["commitContributionsByRepository"]]
    return YearContributions(
        year=year,
        commits=collection["totalCommitContributions"],
        issues=collection["totalIssueContributions"],
        pull_requests=collection["totalPullRequestContributions"],
        restricted=collection["restrictedContributionsCount"],
        has_any=collection["hasAnyContributions"],
        repos=sorted(repos, key=lambda item: -item[1]),
    )


async def contributions_by_year(gh: GitHubAPI, login: str, years: list[int], progress=None) -> list[YearContributions]:
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    done = 0

    async def one(year: int) -> YearContributions:
        nonlocal done
        async with semaphore:
            result = await year_contributions(gh, login, year)
        done += 1
        if progress and done % 3 == 0:
            await progress(f"📊 Reading contributions… {done}/{len(years)}")
        return result

    return sorted(await asyncio.gather(*(one(y) for y in years)), key=lambda c: c.year, reverse=True)


def repos_for_years(entries: list[YearContributions], before_year: int, owner: str) -> list[str]:
    """Repositories owned by `owner` that keep year tabs before `before_year` alive."""
    names: dict[str, int] = {}
    for entry in entries:
        if entry.year >= before_year:
            continue
        for name, count in entry.repos:
            if name.split("/", 1)[0].lower() == owner.lower():
                names[name] = names.get(name, 0) + count
    return [name for name, _ in sorted(names.items(), key=lambda item: -item[1])]


def empty_years(entries: list[YearContributions], before_year: int) -> list[int]:
    return [e.year for e in entries if e.year < before_year and e.commits == 0 and not e.has_any]
