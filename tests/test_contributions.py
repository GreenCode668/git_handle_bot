"""/contributions: per-year contribution data and the repositories behind old year tabs."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ghbot.services.contributions import (
    YearContributions,
    contributions_by_year,
    empty_years,
    repos_for_years,
    year_contributions,
)


def collection(commits, repos, restricted=0):
    return {"user": {"contributionsCollection": {
        "totalCommitContributions": commits, "totalIssueContributions": 0, "totalPullRequestContributions": 0,
        "restrictedContributionsCount": restricted, "hasAnyContributions": bool(commits or restricted),
        "commitContributionsByRepository": [
            {"repository": {"nameWithOwner": name, "isPrivate": False, "owner": {"login": name.split("/")[0]}},
             "contributions": {"totalCount": count}} for name, count in repos],
    }}}


class StubGH:
    def __init__(self, per_year):
        self.per_year = per_year
        self.queries = []
        self.client = SimpleNamespace(graphql=self.graphql)

    async def graphql(self, query, variables):
        year = int(variables["from"][:4])
        self.queries.append(year)
        commits, repos = self.per_year.get(year, (0, []))
        return collection(commits, repos)


async def test_year_contributions_parses_and_sorts_repos():
    gh = StubGH({2017: (60, [("me/small", 12), ("me/big", 48)])})
    entry = await year_contributions(gh, "me", 2017)
    assert entry.commits == 60 and entry.has_any
    assert entry.repos == [("me/big", 48), ("me/small", 12)]
    assert entry.removable  # 48 + 12 == 60: every counted commit is in a repository we can manage


async def test_contributions_by_year_newest_first():
    gh = StubGH({2015: (5, [("me/a", 5)]), 2016: (0, []), 2017: (2, [("me/b", 2)])})
    entries = await contributions_by_year(gh, "me", [2015, 2016, 2017])
    assert [e.year for e in entries] == [2017, 2016, 2015]
    assert sorted(gh.queries) == [2015, 2016, 2017]


def test_repos_for_years_only_owned_and_before_cutoff():
    entries = [
        YearContributions(2022, 10, repos=[("me/old", 7), ("someone/else", 3)]),
        YearContributions(2019, 5, repos=[("me/old", 2), ("me/tiny", 3)]),
        YearContributions(2024, 99, repos=[("me/recent", 99)]),
    ]
    assert repos_for_years(entries, 2023, "me") == ["me/old", "me/tiny"]  # 9 vs 3 commits
    assert repos_for_years(entries, 2020, "me") == ["me/tiny", "me/old"]  # only 2019 counts: 3 vs 2
    assert repos_for_years(entries, 2019, "me") == []


def test_empty_years_are_reported_separately():
    entries = [YearContributions(2014, 0), YearContributions(2015, 3, repos=[("me/a", 3)]), YearContributions(2024, 1)]
    assert empty_years(entries, 2023) == [2014]


@pytest.mark.parametrize("commits,repos,expected", [(10, [("me/a", 10)], True), (10, [("me/a", 4)], False), (0, [], False)])
def test_removable_flag(commits, repos, expected):
    assert YearContributions(2020, commits, repos=repos).removable is expected
