"""Multi-repository analysis: all/selected repos, per-repo backups and confirmations."""

from __future__ import annotations

import shutil

import pytest

from ghbot.services.safety import Stage
from ghbot.validators import RepoRef
from tests.fakes import make_services
from tests.gitutil import commit, git, make_sample_repo

PARAMS = {"cutoff": "2024-01-01", "tag_policy": "snapshot", "squash_style": "none"}


def refs(repo):
    out = git(repo, "for-each-ref", "--format=%(objectname) %(refname)", "refs/heads", "refs/tags")
    return dict(line.split(" ")[::-1] for line in out.splitlines())


@pytest.fixture
def env(tmp_path):
    services, gh, remotes = make_services(tmp_path)
    for name in ("alpha", "beta"):
        root = tmp_path / name
        root.mkdir()
        _, remote = make_sample_repo(root)
        shutil.move(str(remote), remotes / f"{name}.git")
        gh.add_repo(name)
    new = tmp_path / "newonly"
    new.mkdir()
    git(new, "init", "-q", "-b", "main")
    commit(new, "a.txt", "1", "2025-03-01T00:00:00Z")
    git(tmp_path, "clone", "-q", "--mirror", str(new), str(remotes / "fresh.git"))
    gh.add_repo("fresh")
    gh.add_repo("secret", private=True, visibility="private")
    yield services, gh, remotes
    services.db.close()


async def test_batch_analysis_statuses_and_nothing_changed(env):
    services, gh, remotes = env
    before = {n: refs(remotes / f"{n}.git") for n in ("alpha", "beta", "fresh")}
    repos = [RepoRef("me", n) for n in ("alpha", "beta", "fresh", "secret")]
    items = {i.repo.split("/")[1]: i for i in await services.batch.analyze(repos, PARAMS)}
    assert items["alpha"].status == items["beta"].status == "rewrite"
    assert items["alpha"].removed == 4 and items["alpha"].total == 8
    assert items["fresh"].status == "not_needed"
    assert items["secret"].status == "blocked" and "PUBLIC" in items["secret"].detail
    assert {n: refs(remotes / f"{n}.git") for n in before} == before
    assert await services.db.list_locks() == [] and gh.calls == []


async def test_batch_approve_selected_then_confirm_each(env):
    services, gh, remotes = env
    items = await services.batch.analyze([RepoRef("me", "alpha"), RepoRef("me", "beta")], PARAMS)
    alpha, beta = (i.op_id for i in items)
    beta_before = refs(remotes / "beta.git")

    await services.batch.cancel([beta])  # user deselected beta
    results = await services.batch.approve([alpha])
    assert len(results) == 1 and results[0].ok and results[0].phrase == "REWRITE me/alpha"
    assert (await services.db.get_backup(results[0].backup_id)).status == "verified"
    assert (await services.db.get_operation(beta)).stage == Stage.CANCELLED

    done = await services.safety.confirm_phrase("REWRITE me/alpha")
    assert done.stage == Stage.DONE, done.error
    log = git(remotes / "alpha.git", "log", "--format=%s", "main").split()
    assert sorted(log) == ["C", "D", "F2", "M"]  # no summary commit
    assert refs(remotes / "beta.git") == beta_before
    assert await services.safety.confirm_phrase("REWRITE me/beta") is None


async def test_batch_backup_failure_stops_only_that_repo(env, monkeypatch):
    services, gh, remotes = env
    items = await services.batch.analyze([RepoRef("me", "alpha"), RepoRef("me", "beta")], PARAMS)
    real_create = services.backups.create

    async def flaky(repo, reason, operation_id=None):
        if repo.name == "alpha":
            from ghbot.services.backups import BackupError

            raise BackupError("disk full")
        return await real_create(repo, reason, operation_id)

    monkeypatch.setattr(services.backups, "create", flaky)
    results = {r.repo: r for r in await services.batch.approve([i.op_id for i in items])}
    assert not results["me/alpha"].ok and "STOPPED" in results["me/alpha"].detail
    assert results["me/beta"].ok
    assert await services.safety.confirm_phrase("REWRITE me/alpha") is None


async def test_repo_with_only_old_commits_becomes_a_deletion(env):
    """All commits older than the cutoff -> delete the repository instead of leaving snapshots."""
    services, gh, remotes = env
    late = {"cutoff": "2026-01-01", "tag_policy": "snapshot", "squash_style": "none"}
    items = {i.repo.split("/")[1]: i for i in await services.batch.analyze(
        [RepoRef("me", "alpha"), RepoRef("me", "fresh")], late)}
    assert items["alpha"].status == "delete" and items["alpha"].kept == 0
    assert items["fresh"].status == "delete"
    op = await services.db.get_operation(items["alpha"].op_id)
    assert op.kind == "delete_repo" and op.confirm_phrase == "DELETE me/alpha"
    assert (remotes / "alpha.git").exists() and gh.calls == []  # still only analysis

    results = {r.repo: r for r in await services.batch.approve([items["alpha"].op_id])}
    assert results["me/alpha"].ok and results["me/alpha"].phrase == "DELETE me/alpha"
    done = await services.safety.confirm_phrase("DELETE me/alpha")
    assert done.stage == Stage.DONE, done.error
    assert not (remotes / "alpha.git").exists()
    assert (await services.db.get_backup(results["me/alpha"].backup_id)).status == "verified"


async def test_mixed_batch_keeps_repos_that_still_have_commits(env):
    """alpha still has 2024 commits after the cutoff -> rewrite; fresh has nothing older -> untouched."""
    services, gh, remotes = env
    mixed = {"cutoff": "2024-06-01", "tag_policy": "snapshot", "squash_style": "none"}
    items = {i.repo.split("/")[1]: i for i in await services.batch.analyze(
        [RepoRef("me", "alpha"), RepoRef("me", "fresh")], mixed)}
    assert items["alpha"].status == "rewrite" and items["alpha"].kept > 0
    assert items["fresh"].status == "not_needed"
    op = await services.db.get_operation(items["alpha"].op_id)
    assert op.kind == "rewrite_history"


async def test_bulk_deletion_batch(env):
    """Delete several repositories: analysis, per-repo backup, per-repo DELETE phrase."""
    services, gh, remotes = env
    gh.add_repo("secret", private=True, visibility="private")
    items = {i.repo.split("/")[1]: i for i in await services.batch.analyze_deletions(
        [RepoRef("me", n) for n in ("alpha", "beta", "secret", "ghost")])}
    assert items["alpha"].status == items["beta"].status == "delete"
    assert items["secret"].status == "blocked" and "PUBLIC" in items["secret"].detail
    assert items["ghost"].status == "blocked"  # does not exist
    assert gh.calls == [] and (remotes / "alpha.git").exists()  # still nothing changed

    results = {r.repo: r for r in await services.batch.approve([items["alpha"].op_id, items["beta"].op_id])}
    assert all(r.ok for r in results.values())
    assert results["me/alpha"].phrase == "DELETE me/alpha"

    assert (await services.safety.confirm_phrase("DELETE me/alpha")).stage == Stage.DONE
    assert not (remotes / "alpha.git").exists()
    assert (remotes / "beta.git").exists()  # only the confirmed one is gone
    assert (await services.db.get_backup(results["me/beta"].backup_id)).status == "verified"
