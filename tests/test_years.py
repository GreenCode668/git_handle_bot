"""/years: commit-year spans per repository."""

from __future__ import annotations


import pytest

from ghbot.github.client import GitHubError, Page
from ghbot.services.years import YearSpan, collect_year_spans, repo_year_span, year_report


class StubAPI:
    """Minimal list_commits stub: {repo: [newest_year, ..., oldest_year]}."""

    def __init__(self, data: dict[str, list[int] | Exception]) -> None:
        self.data = data
        self.requests: list[tuple[str, int]] = []

    async def list_commits(self, full_name, page=1, per_page=10, sha=None, since=None, until=None) -> Page:
        entry = self.data[full_name]
        if isinstance(entry, Exception):
            raise entry
        self.requests.append((full_name, page))
        if not entry:
            return Page([], page, False, page)
        year = entry[page - 1]
        commit = {"commit": {"committer": {"date": f"{year}-06-01T00:00:00Z"}}}
        return Page([commit], page, page < len(entry), len(entry))


async def test_span_uses_two_requests():
    api = StubAPI({"me/a": [2024, 2020, 2014]})
    span = await repo_year_span(api, "me/a")
    assert (span.first, span.last, span.commits) == (2014, 2024, 3)
    assert api.requests == [("me/a", 1), ("me/a", 3)]


async def test_single_commit_repo_needs_one_request():
    api = StubAPI({"me/a": [2025]})
    span = await repo_year_span(api, "me/a")
    assert (span.first, span.last, span.commits) == (2025, 2025, 1)
    assert api.requests == [("me/a", 1)]


@pytest.mark.parametrize("entry,expect_error", [([], False), (GitHubError(409, "empty"), False), (GitHubError(500, "boom"), True)])
async def test_empty_and_failing_repositories(entry, expect_error):
    span = await repo_year_span(StubAPI({"me/a": entry}), "me/a")
    assert span.first is None
    assert bool(span.error) is expect_error


async def test_collect_sorts_oldest_first():
    api = StubAPI({"me/new": [2025], "me/old": [2024, 2013], "me/empty": []})
    spans = await collect_year_spans(api, ["me/new", "me/old", "me/empty"])
    assert [s.repo for s in spans] == ["me/old", "me/new", "me/empty"]


def test_year_report_flags_years_before_account():
    spans = [YearSpan("me/old", 2013, 2015, 20), YearSpan("me/mid", 2022, 2024, 5),
             YearSpan("me/new", 2024, 2026, 3), YearSpan("me/empty")]
    report = year_report(spans, created_year=2023)
    assert report["old_years"] == [2022, 2015, 2014, 2013]
    assert list(report["culprits"]) == ["me/old", "me/mid"]
    assert report["culprits"]["me/old"] == [2015, 2014, 2013]
    assert report["culprits"]["me/mid"] == [2022]
    assert report["by_year"][2024] == ["me/mid", "me/new"]
    assert report["empty"] == ["me/empty"] and report["oldest"] == 2013


def test_year_report_when_nothing_is_older():
    report = year_report([YearSpan("me/a", 2024, 2026, 4)], created_year=2023)
    assert report["old_years"] == [] and report["culprits"] == {}
