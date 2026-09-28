"""Read-only commit-history statistics from a blobless clone."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ghbot.git.runner import GitRunner

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass
class HistoryStats:
    total_commits: int
    merges: int
    roots: int
    authors: int
    first_commit: str | None
    last_commit: str | None
    per_year: dict[str, int] = field(default_factory=dict)
    top_authors: list[tuple[str, int]] = field(default_factory=list)
    busiest_weekday: str | None = None
    branches: int = 0
    tags: int = 0


def blobless_clone(git: GitRunner, url: str, dest: Path, *, auth: bool = True) -> None:
    """Bare clone with commits and trees only; enough for history statistics."""
    git.run(["clone", "--bare", "--filter=blob:none", "--no-hardlinks", "--", url, str(dest)], auth=auth)


def history_stats(git: GitRunner, repo: Path) -> HistoryStats:
    refs = git.run(["for-each-ref", "--format=%(refname)", "refs/heads", "refs/tags"], cwd=repo).stdout.split()
    branches = sum(1 for r in refs if r.startswith("refs/heads/"))
    tags = sum(1 for r in refs if r.startswith("refs/tags/"))
    if not refs:
        return HistoryStats(0, 0, 0, 0, None, None, branches=0, tags=0)
    out = git.run(["log", "--branches", "--tags", "--format=%ct%x00%aN%x00%P"], cwd=repo).stdout
    times: list[int] = []
    authors: Counter[str] = Counter()
    years: Counter[str] = Counter()
    weekdays: Counter[str] = Counter()
    merges = roots = 0
    for line in out.splitlines():
        ts, author, parents = line.split("\x00")
        stamp = int(ts)
        when = datetime.fromtimestamp(stamp, UTC)
        times.append(stamp)
        authors[author] += 1
        years[str(when.year)] += 1
        weekdays[WEEKDAYS[when.weekday()]] += 1
        count = len(parents.split())
        merges += count > 1
        roots += count == 0
    fmt = "%Y-%m-%d"
    return HistoryStats(
        total_commits=len(times),
        merges=merges,
        roots=roots,
        authors=len(authors),
        first_commit=datetime.fromtimestamp(min(times), UTC).strftime(fmt) if times else None,
        last_commit=datetime.fromtimestamp(max(times), UTC).strftime(fmt) if times else None,
        per_year=dict(sorted(years.items())),
        top_authors=authors.most_common(5),
        busiest_weekday=weekdays.most_common(1)[0][0] if weekdays else None,
        branches=branches,
        tags=tags,
    )
