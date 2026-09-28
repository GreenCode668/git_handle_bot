from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ghbot.git.history import (
    analyze_history,
    author_stats,
    build_push_args,
    rewrite_authors,
    rewrite_history,
)
from ghbot.git.mirror import important, local_refs, mirror_clone
from ghbot.git.runner import GitError
from tests.gitutil import commit, git, make_sample_repo

CUTOFF = datetime(2024, 1, 1, tzinfo=UTC)


def snapshot(repo: Path) -> dict[str, str]:
    return local_refs_raw(repo)


def local_refs_raw(repo: Path) -> dict[str, str]:
    out = git(repo, "for-each-ref", "--format=%(objectname) %(refname)")
    return {n: s for s, _, n in (l.partition(" ") for l in out.splitlines())}


@pytest.fixture
def sample(tmp_path, git_runner):
    work, remote = make_sample_repo(tmp_path)
    mirror = tmp_path / "mirror.git"
    mirror_clone(git_runner, str(remote), mirror, auth=False, local=True)
    return work, remote, mirror


def test_analysis_counts_and_is_read_only(sample, git_runner):
    _, _, mirror = sample
    before = snapshot(mirror)
    a = analyze_history(git_runner, mirror, CUTOFF)
    assert snapshot(mirror) == before  # nothing modified
    assert a.total_commits == 8  # A B F1 F2 C M D O1
    assert a.dated_before == 4  # A B F1 O1
    assert a.dated_after == 4
    assert a.squash_count == 4
    assert a.rewrite_required
    names = {b.name: b for b in a.branches}
    assert names["old-branch"].tip_is_old
    assert names["main"].old_commits == 3  # A B F1
    statuses = {t.name: t.status for t in a.tags}
    assert statuses == {"v0.1": "old", "v1.0": "rewritten", "v2.0": "rewritten"}
    assert a.merges_crossing_cutoff == 0
    assert not a.blockers


def test_cutoff_before_history_needs_no_rewrite(sample, git_runner):
    _, _, mirror = sample
    a = analyze_history(git_runner, mirror, datetime(2020, 1, 1, tzinfo=UTC))
    assert a.squash_count == 0
    assert not a.rewrite_required
    with pytest.raises(GitError):
        rewrite_history(git_runner, mirror, datetime(2020, 1, 1, tzinfo=UTC))


@pytest.mark.parametrize("policy", ["snapshot", "drop"])
def test_rewrite_preserves_files_and_removes_old_commits(sample, git_runner, policy):
    _, remote, mirror = sample
    old_refs = important(local_refs(git_runner, mirror))
    old_trees = {r: git(mirror, "rev-parse", f"{s}^{{tree}}").strip() for r, s in old_refs.items() if r.startswith("refs/heads/")}

    result = rewrite_history(git_runner, mirror, CUTOFF, policy)

    new_refs = important(local_refs(git_runner, mirror))
    for ref, tree in old_trees.items():
        assert git(mirror, "rev-parse", f"{new_refs[ref]}^{{tree}}").strip() == tree
    # main history: squash root(B) -> F2', C' -> M' -> D'
    log = git(mirror, "log", "--format=%s", "refs/heads/main").splitlines()
    assert log[:2] == ["D", "M"] and set(log[2:4]) == {"C", "F2"}
    assert log[-1].startswith("Squashed history before 2024-01-01")
    assert "A" not in log and "B" not in log
    # author dates preserved
    assert git(mirror, "log", "-1", "--format=%aI", "refs/heads/main").strip().startswith("2024-09-01")
    if policy == "snapshot":
        assert "refs/tags/v0.1" in new_refs
        assert git(mirror, "cat-file", "-t", "refs/tags/v0.1").strip() == "tag"
    else:
        assert "refs/tags/v0.1" not in new_refs
    assert git(mirror, "cat-file", "-t", "refs/tags/v2.0").strip() == "tag"
    assert result.removed >= 1

    # Push to the fake remote with leases and confirm it matches.
    git_runner.run(build_push_args(str(remote), result.updates), cwd=mirror, local=True)
    assert important(local_refs_raw(remote)) == result.expected_refs


def test_push_lease_rejects_changed_remote(sample, git_runner, tmp_path):
    work, remote, mirror = sample
    result = rewrite_history(git_runner, mirror, CUTOFF)
    # Someone pushes to main after the backup/analysis.
    commit(work, "late.txt", "x", "2024-10-01T00:00:00Z")
    git(work, "push", "-q", str(remote), "main")
    before = local_refs_raw(remote)
    with pytest.raises(GitError):
        git_runner.run(build_push_args(str(remote), result.updates), cwd=mirror, local=True)
    assert local_refs_raw(remote) == before  # atomic: nothing changed


def test_push_args_never_force_refspecs():
    from ghbot.git.history import RefUpdate
    args = build_push_args("https://github.com/o/r.git", [RefUpdate("refs/heads/main", "a" * 40, "b" * 40),
                                                         RefUpdate("refs/tags/x", "c" * 40, None)])
    refspecs = args[args.index("--") + 2:]
    assert refspecs == ["b" * 40 + ":refs/heads/main", ":refs/tags/x"]
    assert "--atomic" in args and "--force" not in args


def test_date_skew_commits_are_kept(tmp_path, git_runner):
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    commit(repo, "a", "1", "2023-01-01T00:00:00Z")
    commit(repo, "a", "2", "2024-06-01T00:00:00Z")
    commit(repo, "a", "3", "2023-03-01T00:00:00Z", "skewed")  # older date, newer position
    a = analyze_history(git_runner, repo / ".git", CUTOFF)
    assert a.squash_count == 1
    assert a.date_anomalies == 1
    assert any("clock skew" in c for c in a.complications)


def test_no_summary_style_removes_old_commits_entirely(sample, git_runner):
    _, remote, mirror = sample
    old_refs = important(local_refs(git_runner, mirror))
    old_trees = {r: git(mirror, "rev-parse", f"{s}^{{tree}}").strip() for r, s in old_refs.items() if r.startswith("refs/heads/")}
    analysis = analyze_history(git_runner, mirror, CUTOFF, "drop", "none")
    assert analysis.rewrite_required and analysis.squash_style == "none"
    assert any("old-branch" in c for c in analysis.complications)  # no post-cutoff commits -> snapshot kept

    result = rewrite_history(git_runner, mirror, CUTOFF, "drop", "none")
    new_refs = important(local_refs(git_runner, mirror))
    for ref, tree in old_trees.items():
        assert git(mirror, "rev-parse", f"{new_refs[ref]}^{{tree}}").strip() == tree
    main_log = git(mirror, "log", "--format=%s", "refs/heads/main").splitlines()
    assert sorted(main_log) == ["C", "D", "F2", "M"]  # 8 commits -> no A/B/F1 and no summary commit
    assert not any("Squashed" in line or "Snapshot" in line for line in main_log)
    roots = git(mirror, "rev-list", "--max-parents=0", "refs/heads/main").split()
    assert len(roots) == 2  # F2 and C both started from pre-cutoff history
    old_branch = git(mirror, "log", "--format=%s", "refs/heads/old-branch").splitlines()
    assert len(old_branch) == 1 and old_branch[0].startswith("Snapshot of history before")
    assert "refs/tags/v0.1" not in new_refs
    git_runner.run(build_push_args(str(remote), result.updates), cwd=mirror, local=True)
    assert important(local_refs_raw(remote)) == result.expected_refs


def test_no_summary_style_linear_history(tmp_path, git_runner):
    repo = tmp_path / "lin"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "master")
    for i, day in enumerate(["2014-01-01", "2015-01-01", "2016-02-01", "2017-01-01"]):
        commit(repo, f"f{i}.txt", str(i), f"{day}T00:00:00Z", f"c{i}")
    tree = git(repo, "rev-parse", "master^{tree}").strip()
    mirror = tmp_path / "lin.git"
    mirror_clone(git_runner, str(repo), mirror, auth=False, local=True)
    cutoff = datetime(2016, 1, 1, tzinfo=UTC)
    rewrite_history(git_runner, mirror, cutoff, "snapshot", "none")
    assert git(mirror, "log", "--format=%s", "master").split() == ["c3", "c2"]
    assert git(mirror, "rev-parse", "master^{tree}").strip() == tree
    assert set(git(mirror, "ls-tree", "--name-only", "master~1").split()) == {"f0.txt", "f1.txt", "f2.txt"}


def build_multi_author_repo(tmp_path):
    """Repo with commits by two identities, one annotated tag and a merge."""
    repo = tmp_path / "multi"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    env = {"other": ("Yuri", "yuri@example.com"), "mine": ("Old Name", "old@example.com")}

    def author_commit(who, filename, content, date, message):
        (repo / filename).write_text(content)
        git(repo, "add", filename)
        git(repo, "commit", "-q", "-m", message, date=date, author=env[who])

    author_commit("mine", "a.txt", "1", "2024-01-01T00:00:00Z", "mine one")
    author_commit("other", "b.txt", "2", "2024-02-01T00:00:00Z", "theirs one")
    git(repo, "checkout", "-q", "-b", "side")
    author_commit("mine", "c.txt", "3", "2024-03-01T00:00:00Z", "mine two")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "side", "-m", "merge", date="2024-04-01T00:00:00Z", author=env["mine"])
    git(repo, "tag", "-a", "v1", "-m", "release", author=env["mine"])
    return repo


def test_author_stats_lists_identities(tmp_path, git_runner):
    repo = build_multi_author_repo(tmp_path)
    stats = {(a.name, a.email): a.commits for a in author_stats(git_runner, repo / ".git")}
    assert stats[("Old Name", "old@example.com")] == 3  # 2 commits + the merge
    assert stats[("Yuri", "yuri@example.com")] == 1


def test_rewrite_authors_only_touches_selected_identity(tmp_path, git_runner):
    repo = build_multi_author_repo(tmp_path)
    mirror = tmp_path / "multi.git"
    mirror_clone(git_runner, str(repo), mirror, auth=False, local=True)
    before_tree = git(mirror, "rev-parse", "main^{tree}").strip()
    before_dates = git(mirror, "log", "--format=%aI %s", "main")

    result = rewrite_authors(git_runner, mirror, {("Old Name", "old@example.com")},
                             "trator0117", "colin@example.com")

    assert result.removed == 3  # commits whose author changed
    assert git(mirror, "rev-parse", "main^{tree}").strip() == before_tree
    assert git(mirror, "log", "--format=%aI %s", "main") == before_dates  # dates and messages intact
    authors = {(a.name, a.email): a.commits for a in author_stats(git_runner, mirror)}
    assert authors == {("trator0117", "colin@example.com"): 3, ("Yuri", "yuri@example.com"): 1}
    # the other author's commit keeps its identity, including as committer
    assert "Yuri" in git(mirror, "log", "--format=%an %cn", "main")
    assert git(mirror, "cat-file", "-t", "refs/tags/v1").strip() == "tag"
    assert "trator0117" in git(mirror, "cat-file", "-p", "refs/tags/v1")


def test_rewrite_authors_rejects_unknown_identity(tmp_path, git_runner):
    repo = build_multi_author_repo(tmp_path)
    mirror = tmp_path / "m2.git"
    mirror_clone(git_runner, str(repo), mirror, auth=False, local=True)
    with pytest.raises(GitError):
        rewrite_authors(git_runner, mirror, set(), "x", "x@example.com")
    with pytest.raises(GitError):
        rewrite_authors(git_runner, mirror, {("Old Name", "old@example.com")}, "x", "not-an-email")
