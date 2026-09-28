"""Public-only policy, manual protection, dry runs, new operations and bulk backups."""

from __future__ import annotations

import dataclasses
import shutil

import pytest

from ghbot.git.stats import history_stats
from ghbot.services.policy import PolicyError
from ghbot.services.safety import SafetyError, Stage
from ghbot.validators import RepoRef
from tests.fakes import make_services
from tests.gitutil import commit, git, make_sample_repo

REPO = RepoRef("me", "proj")


@pytest.fixture
def env(tmp_path):
    services, gh, remotes = make_services(tmp_path)
    work, remote = make_sample_repo(tmp_path)
    shutil.move(str(remote), remotes / "proj.git")
    gh.add_repo("proj")
    yield services, gh, remotes, work
    services.db.close()


def make_private(gh, name="proj"):
    gh.repos[name]["private"] = True
    gh.repos[name]["visibility"] = "private"


# ----------------------------------------------------------------- policy
@pytest.mark.parametrize("kind,params", [
    ("delete_repo", {}), ("rename_repo", {"new_name": "x"}), ("archive_repo", {}), ("make_public", {}),
    ("rewrite_history", {"cutoff": "2024-01-01"}), ("set_description", {"value": "hi"}),
    ("set_topics", {"topics": ["a"]}), ("set_homepage", {"value": "https://example.com"}),
])
async def test_private_repositories_are_never_modified(env, kind, params):
    services, gh, remotes, _ = env
    make_private(gh)
    with pytest.raises(SafetyError, match="Only PUBLIC"):
        await services.safety.start(kind, REPO, params)
    assert gh.calls == [] and (remotes / "proj.git").exists()


async def test_repo_turning_private_after_backup_blocks_execution(env):
    services, gh, remotes, _ = env
    op = await services.safety.approve((await services.safety.start("delete_repo", REPO, {})).id)
    assert op.stage == Stage.AWAITING_CONFIRMATION
    make_private(gh)  # visibility changes on GitHub between backup and confirmation
    with pytest.raises(SafetyError, match="Nothing was changed"):
        await services.safety.confirm_phrase("DELETE me/proj")
    assert (remotes / "proj.git").exists() and ("delete", "me/proj") not in gh.calls
    assert (await services.db.get_operation(op.id)).stage == Stage.FAILED
    assert await services.db.list_locks() == []


async def test_repo_not_owned_is_refused(env):
    services, gh, *_ = env
    gh.repos["proj"]["owner"] = {"login": "someone-else"}
    with pytest.raises(SafetyError, match="not owned"):
        await services.safety.start("delete_repo", REPO, {})


async def test_backups_of_private_repositories_are_refused(env):
    services, gh, *_ = env
    make_private(gh)
    from ghbot.services.backups import BackupError

    with pytest.raises(BackupError, match="PUBLIC"):
        await services.backups.create(REPO, "manual")


async def test_private_repos_hidden_from_read_views_by_default(env):
    services, gh, *_ = env
    make_private(gh)
    with pytest.raises(PolicyError, match="not found or is not managed"):
        await services.policy.readable(REPO)
    services.settings = dataclasses.replace(services.settings, show_private_repos=True)
    services.policy.settings = services.settings
    assert (await services.policy.readable(REPO))["name"] == "proj"
    # ...but still never writable, even when shown
    with pytest.raises(SafetyError):
        await services.safety.start("archive_repo", REPO, {})


async def test_import_and_create_only_public(env):
    services, *_ = env
    with pytest.raises(SafetyError, match="only creates PUBLIC"):
        await services.safety.start("import_repo", RepoRef("me", "new"), {"url": "https://example.com/a/b.git", "private": True})
    op = await services.safety.start("create_repo", RepoRef("me", "fresh"), {"description": "demo"})
    done = await services.safety.confirm_button(op.id)
    assert done.stage == Stage.DONE, done.error
    assert services.gh.repos["fresh"]["private"] is False


async def test_restore_of_private_backup_into_missing_repo_is_refused(env):
    services, gh, remotes, _ = env
    record = await services.backups.create_verified(REPO, "manual")
    meta_file = services.settings.backup_path / record.id / "metadata.json"
    meta_file.write_text(meta_file.read_text().replace('"private": false', '"private": true'))
    with pytest.raises(SafetyError, match="only creates PUBLIC"):
        await services.safety.start("restore_backup", RepoRef("me", "gone"), {"backup_id": record.id})


# ------------------------------------------------------------- protection
async def test_manual_protection_blocks_writes_until_unlocked(env):
    services, gh, remotes, _ = env
    await services.db.protect_repo("me/proj", "release freeze")
    for kind, params in (("delete_repo", {}), ("rename_repo", {"new_name": "x"})):
        with pytest.raises(SafetyError, match="manually protected"):
            await services.safety.start(kind, REPO, params)
    unlock = await services.safety.start("unlock_repo", REPO, {})
    done = await services.safety.confirm_button(unlock.id)
    assert done.stage == Stage.DONE
    assert await services.db.get_protection("me/proj") is None
    assert (await services.safety.start("delete_repo", REPO, {})).stage == Stage.ANALYZED


async def test_protection_added_while_pending_blocks_execution(env):
    services, gh, remotes, _ = env
    await services.safety.approve((await services.safety.start("delete_repo", REPO, {})).id)
    await services.db.protect_repo("me/proj", None)
    with pytest.raises(SafetyError, match="protected"):
        await services.safety.confirm_phrase("DELETE me/proj")
    assert (remotes / "proj.git").exists()


# ---------------------------------------------------------------- dry run
async def test_dry_run_changes_nothing(env):
    services, gh, remotes, _ = env
    op = await services.safety.dry_run("rewrite_history", REPO, {"cutoff": "2024-01-01", "tag_policy": "snapshot"})
    assert op.kind == "dryrun" and op.result["would_proceed"] is True
    assert await services.db.list_locks() == []
    _, total = await services.db.list_backups(limit=1)
    assert total == 0 and gh.calls == []
    assert await services.db.operations_in_stages([Stage.ANALYZED, Stage.AWAITING_CONFIRMATION]) == []

    make_private(gh)
    blocked = await services.safety.dry_run("delete_repo", REPO, {})
    assert blocked.result["would_proceed"] is False
    assert (remotes / "proj.git").exists()


# ------------------------------------------------------ metadata & actions
async def test_topics_and_description_operations(env):
    services, gh, *_ = env
    op = await services.safety.start("set_topics", REPO, {"topics": ["python", "bot"]})
    assert "Added: bot, python" in op.impact["lines"]
    assert (await services.safety.confirm_button(op.id)).stage == Stage.DONE
    assert gh.repos["proj"]["topics"] == ["bot", "python"]
    op = await services.safety.start("set_description", REPO, {"value": "A demo"})
    assert (await services.safety.confirm_button(op.id)).stage == Stage.DONE
    assert gh.repos["proj"]["description"] == "A demo"


async def test_actions_rerun_and_cancel_follow_safety_flow(env):
    services, gh, *_ = env
    gh.runs[7] = {"id": 7, "name": "CI", "run_number": 3, "status": "completed", "conclusion": "failure",
                  "run_attempt": 1, "head_branch": "main", "head_sha": "abc1234"}
    gh.runs[8] = {**gh.runs[7], "id": 8, "status": "in_progress", "conclusion": None}
    with pytest.raises(SafetyError):
        await services.safety.confirm_button((await services.safety.start("actions_cancel", REPO, {"run_id": 7})).id)
    rerun = await services.safety.confirm_button((await services.safety.start("actions_rerun_failed", REPO, {"run_id": 7})).id)
    assert rerun.stage == Stage.DONE and ("rerun", 7) in gh.calls
    cancel = await services.safety.confirm_button((await services.safety.start("actions_cancel", REPO, {"run_id": 8})).id)
    assert cancel.stage == Stage.DONE and ("cancel", 8) in gh.calls
    make_private(gh)
    with pytest.raises(SafetyError, match="Only PUBLIC"):
        await services.safety.start("actions_rerun", REPO, {"run_id": 7})


# ------------------------------------------------------------------- bulk
async def test_backup_all_skips_unchanged_and_diff(env):
    services, gh, remotes, work = env
    first = await services.bulk.run(await services.bulk.managed_repos(), reason="backup-all", skip_unchanged=True)
    assert len(first.created) == 1 and not first.failed
    second = await services.bulk.run(await services.bulk.managed_repos(), reason="backup-all", skip_unchanged=True)
    assert len(second.unchanged) == 1 and not second.created
    backup_id = first.created[0].split("(")[1].rstrip(")")
    assert (await services.bulk.diff(backup_id))["identical"]
    commit(work, "n.txt", "n", "2025-01-01T00:00:00Z")
    git(work, "push", "-q", str(remotes / "proj.git"), "main")
    diff = await services.bulk.diff(backup_id)
    assert not diff["identical"] and diff["changed"][0]["ref"] == "refs/heads/main"


async def test_bulk_skips_private_and_locked(env):
    services, gh, remotes, _ = env
    gh.add_repo("secret", private=True, visibility="private")
    assert [r["name"] for r in await services.bulk.managed_repos()] == ["proj"]
    await services.db.acquire_lock("me/proj", "other op", 99)
    result = await services.bulk.run(await services.bulk.managed_repos(), reason="backup-all", skip_unchanged=False)
    assert result.skipped and not result.created


async def test_prune_only_removes_old_automatic_backups(env):
    services, gh, remotes, work = env
    manual = await services.backups.create_verified(REPO, "manual")
    autos = []
    for i in range(3):
        commit(work, "p.txt", str(i), f"2025-02-0{i + 1}T00:00:00Z")
        git(work, "push", "-q", str(remotes / "proj.git"), "main")
        autos.append((await services.backups.create_verified(REPO, "automatic")).id)
    pruned = await services.bulk.prune_automatic(keep=1)
    assert set(pruned) == set(autos[:2])
    assert (await services.db.get_backup(manual.id)).status == "verified"
    assert (await services.db.get_backup(autos[2])).status == "verified"


async def test_verify_all_reports_missing(env):
    services, *_ = env
    a = await services.backups.create_verified(REPO, "manual")
    b = await services.backups.create_verified(REPO, "manual")
    shutil.rmtree(services.settings.backup_path / b.id)
    report = await services.bulk.verify_all()
    assert report["ok"] == [a.id] and len(report["missing"]) == 1


def test_history_stats(tmp_path, git_runner):
    _, remote = make_sample_repo(tmp_path)
    stats = history_stats(git_runner, remote)
    assert stats.total_commits == 8 and stats.merges == 1 and stats.branches == 3 and stats.tags == 3
    assert stats.first_commit == "2023-01-10" and stats.last_commit == "2024-09-01"


async def test_reauthor_flow_maps_only_selected_identity(tmp_path):
    """Author rewrite: full safety flow, other authors untouched, files intact."""
    from tests.gitutil import git as raw_git

    services, gh, remotes = make_services(tmp_path)
    try:
        work = tmp_path / "authored"
        work.mkdir()
        raw_git(work, "init", "-q", "-b", "main")
        for i, who in enumerate([("Yuri", "yuri@example.com"), ("Yuri", "yuri@example.com"), ("Someone", "other@example.com")]):
            (work / f"f{i}.txt").write_text(str(i))
            raw_git(work, "add", f"f{i}.txt")
            raw_git(work, "commit", "-q", "-m", f"c{i}", date=f"2024-0{i + 1}-01T00:00:00Z", author=who)
        raw_git(tmp_path, "clone", "-q", "--mirror", str(work), str(remotes / "proj.git"))
        gh.add_repo("proj")
        repo = RepoRef("me", "proj")
        tree_before = raw_git(remotes / "proj.git", "rev-parse", "main^{tree}").strip()

        params = {"identities": [["Yuri", "yuri@example.com"]], "new_name": "trator0117",
                  "new_email": "colin@example.com"}
        op = await services.safety.start("reauthor_repo", repo, params)
        assert op.stage == Stage.ANALYZED, op.error
        assert "Commits to re-author: 2" in op.impact["lines"]
        assert any("licences" in w for w in op.impact["warnings"])

        op = await services.safety.approve(op.id)
        assert op.backup_id and op.confirm_phrase == "REAUTHOR me/proj"
        done = await services.safety.confirm_phrase("REAUTHOR me/proj")
        assert done.stage == Stage.DONE, done.error
        assert done.result["commits_reauthored"] == 2

        log = raw_git(remotes / "proj.git", "log", "--format=%an <%ae> %s", "main").splitlines()
        assert log == ["Someone <other@example.com> c2", "trator0117 <colin@example.com> c1",
                       "trator0117 <colin@example.com> c0"]
        assert raw_git(remotes / "proj.git", "rev-parse", "main^{tree}").strip() == tree_before
        assert raw_git(remotes / "proj.git", "log", "--format=%aI", "main").split()[-1].startswith("2024-01-01")
    finally:
        services.db.close()


async def test_reauthor_refuses_identity_with_no_commits(tmp_path):
    services, gh, remotes = make_services(tmp_path)
    try:
        from tests.gitutil import make_sample_repo

        _, remote = make_sample_repo(tmp_path / "s" if (tmp_path / "s").mkdir() is None else tmp_path)
        shutil.move(str(remote), remotes / "proj.git")
        gh.add_repo("proj")
        op = await services.safety.start("reauthor_repo", RepoRef("me", "proj"), {
            "identities": [["Nobody", "nobody@example.com"]], "new_name": "me", "new_email": "me@example.com"})
        assert op.stage == Stage.CANCELLED and "No commits use the selected identities." in op.impact["blockers"]
    finally:
        services.db.close()
