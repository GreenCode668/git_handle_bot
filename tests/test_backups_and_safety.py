from __future__ import annotations

import json
import shutil

import pytest

from ghbot.git.mirror import important
from ghbot.services.backups import BackupError
from ghbot.services.safety import SafetyError, Stage
from ghbot.validators import RepoRef
from tests.fakes import make_services
from tests.gitutil import commit, git, make_sample_repo


def refs(repo) -> dict[str, str]:
    out = git(repo, "for-each-ref", "--format=%(objectname) %(refname)")
    return important({n: s for s, _, n in (l.partition(" ") for l in out.splitlines())})


@pytest.fixture
def env(tmp_path):
    services, gh, remotes = make_services(tmp_path)
    work, remote = make_sample_repo(tmp_path)
    shutil.move(str(remote), remotes / "proj.git")
    gh.add_repo("proj")
    yield services, gh, remotes, work
    services.db.close()


REPO = RepoRef("me", "proj")


async def test_backup_create_and_verify(env):
    services, _, remotes, _ = env
    record = await services.backups.create(REPO, "manual")
    assert record.id.startswith("BK-") and record.id.endswith("-001")
    report = await services.backups.verify(record.id)
    assert report.ok, report.failed()
    assert (await services.db.get_backup(record.id)).status == "verified"
    manifest = json.loads((services.settings.backup_path / record.id / "manifest.json").read_text())
    assert important(manifest["refs"]) == refs(remotes / "proj.git")
    ok, _ = await services.backups.remote_matches(REPO.full_name, record.id)
    assert ok


async def test_tampered_backup_fails_verification(env):
    services, *_ = env
    record = await services.backups.create(REPO, "manual")
    bundle = services.settings.backup_path / record.id / "repo.bundle"
    bundle.write_bytes(bundle.read_bytes()[:-10])
    report = await services.backups.verify(record.id)
    assert not report.ok
    assert (await services.db.get_backup(record.id)).status == "failed"


async def test_delete_flow_requires_backup_and_phrase(env):
    services, gh, remotes, _ = env
    engine = services.safety
    op = await engine.start("delete_repo", REPO, {})
    assert op.stage == Stage.ANALYZED
    # Wrong phrase / not yet approved: nothing happens.
    assert await engine.confirm_phrase("DELETE me/proj") is None
    op = await engine.approve(op.id)
    assert op.stage == Stage.AWAITING_CONFIRMATION and op.backup_id
    assert (await services.db.get_backup(op.backup_id)).status == "verified"
    assert await engine.confirm_phrase("delete me/proj") is None  # case-sensitive
    done = await engine.confirm_phrase("DELETE me/proj")
    assert done.stage == Stage.DONE
    assert not (remotes / "proj.git").exists()
    assert await services.db.list_locks() == []


async def test_backup_failure_stops_before_any_change(env, monkeypatch):
    services, gh, remotes, _ = env
    op = await services.safety.start("delete_repo", REPO, {})

    async def boom(*a, **k):
        raise BackupError("disk full")

    monkeypatch.setattr(services.backups, "create", boom)
    with pytest.raises(SafetyError, match="STOPPED"):
        await services.safety.approve(op.id)
    assert (await services.db.get_operation(op.id)).stage == Stage.FAILED
    assert gh.calls == [] and (remotes / "proj.git").exists()
    assert await services.db.list_locks() == []


async def test_backup_invalidated_before_execution_blocks(env):
    services, gh, remotes, _ = env
    op = await services.safety.approve((await services.safety.start("delete_repo", REPO, {})).id)
    shutil.rmtree(services.settings.backup_path / op.backup_id / "repo.git")
    with pytest.raises(SafetyError, match="Nothing was changed"):
        await services.safety.confirm_phrase("DELETE me/proj")
    assert (remotes / "proj.git").exists() and gh.calls == []


async def test_remote_changed_after_backup_blocks(env):
    services, gh, remotes, work = env
    await services.safety.approve((await services.safety.start("delete_repo", REPO, {})).id)
    commit(work, "new.txt", "n", "2025-01-01T00:00:00Z")
    git(work, "push", "-q", str(remotes / "proj.git"), "main")
    with pytest.raises(SafetyError, match="changed since backup"):
        await services.safety.confirm_phrase("DELETE me/proj")
    assert (remotes / "proj.git").exists()


async def test_lock_prevents_concurrent_destructive_ops(env):
    services, *_ = env
    first = await services.safety.start("delete_repo", REPO, {})
    second = await services.safety.start("rename_repo", REPO, {"new_name": "other"})
    await services.safety.approve(first.id)
    with pytest.raises(SafetyError, match="locked"):
        await services.safety.confirm_button(second.id)
    await services.safety.cancel(first.id)
    done = await services.safety.confirm_button(second.id)
    assert done.stage == Stage.DONE


async def test_double_approve_is_rejected(env):
    services, *_ = env
    op = await services.safety.start("delete_repo", REPO, {})
    await services.safety.approve(op.id)
    with pytest.raises(SafetyError):
        await services.safety.approve(op.id)


async def test_rewrite_and_undo_via_restore(env):
    services, gh, remotes, _ = env
    original = refs(remotes / "proj.git")
    op = await services.safety.start("rewrite_history", REPO, {"cutoff": "2024-01-01", "tag_policy": "snapshot"})
    assert op.stage == Stage.ANALYZED, op.error
    assert op.impact["data"]["analysis"]["rewrite_required"]
    op = await services.safety.approve(op.id)
    done = await services.safety.confirm_phrase("REWRITE me/proj")
    assert done.stage == Stage.DONE, done.error
    rewritten = refs(remotes / "proj.git")
    assert rewritten != original
    log = git(remotes / "proj.git", "log", "--format=%s", "main")
    assert "\nA\n" not in f"\n{log}" and "Squashed history" in log

    # Undo: restore the pre-rewrite backup.
    restore = await services.safety.start("restore_backup", REPO, {"backup_id": op.backup_id})
    assert restore.stage == Stage.ANALYZED, restore.error
    restore = await services.safety.approve(restore.id)
    assert restore.backup_id and restore.backup_id != op.backup_id  # safety backup of rewritten state
    finished = await services.safety.confirm_phrase("RESTORE me/proj")
    assert finished.stage == Stage.DONE, finished.error
    assert refs(remotes / "proj.git") == original


async def test_restore_deleted_repository(env):
    services, gh, remotes, _ = env
    original = refs(remotes / "proj.git")
    op = await services.safety.approve((await services.safety.start("delete_repo", REPO, {})).id)
    await services.safety.confirm_phrase("DELETE me/proj")
    restore = await services.safety.start("restore_backup", REPO, {"backup_id": op.backup_id})
    assert not restore.impact["target_exists"]
    restore = await services.safety.approve(restore.id)
    assert restore.backup_id is None  # nothing exists that could be damaged
    finished = await services.safety.confirm_phrase("RESTORE me/proj")
    assert finished.stage == Stage.DONE, finished.error
    assert refs(remotes / "proj.git") == original


async def test_expired_confirmation_does_nothing(env):
    services, gh, remotes, _ = env
    await services.db.set_setting("confirmation_ttl_minutes", -1)
    op = await services.safety.start("delete_repo", REPO, {})
    with pytest.raises(SafetyError, match="expired"):
        await services.safety.approve(op.id)
    assert (remotes / "proj.git").exists()


async def test_startup_recovery_marks_interrupted(env):
    services, *_ = env
    op = await services.safety.start("delete_repo", REPO, {})
    await services.db.transition(op.id, [Stage.ANALYZED], Stage.EXECUTING)
    await services.db.acquire_lock(REPO.key, "x", op.id)
    recovered = await services.safety.recover_on_startup()
    assert [o.id for o in recovered] == [op.id]
    assert (await services.db.get_operation(op.id)).stage == Stage.INTERRUPTED
    assert await services.db.list_locks() == []


async def test_operation_rendering_all_stages(env):
    from types import SimpleNamespace

    from ghbot.bot.handlers.operations import render_operation

    services, *_ = env
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"services": services}))
    op = await services.safety.start("rewrite_history", REPO, {"cutoff": "2024-01-01", "tag_policy": "snapshot"})
    text, markup = render_operation(context, op)
    assert "Total commits: 8" in text and "Continue: backup" in str(markup)
    op = await services.safety.approve(op.id)
    text, _ = render_operation(context, op)
    assert "REWRITE me/proj" in text and op.backup_id in text
    done = await services.safety.confirm_phrase("REWRITE me/proj")
    text, _ = render_operation(context, done)
    assert "Completed and verified" in text and "/undo" in text
    for kind, params in (("rename_repo", {"new_name": "x"}), ("make_private", {}), ("delete_repo", {})):
        o = await services.safety.start(kind, REPO, params)
        text, markup = render_operation(context, o)
        assert "<b>" in text and markup is not None
        await services.safety.cancel(o.id)
        text, _ = render_operation(context, await services.db.get_operation(o.id))
        assert "CANCELLED" in text


async def test_recent_verified_backup_is_reused(env):
    services, gh, remotes, work = env
    first = await services.safety.approve((await services.safety.start("delete_repo", REPO, {})).id)
    await services.safety.cancel(first.id)  # confirmation expired / cancelled: backup stays

    second = await services.safety.approve((await services.safety.start("delete_repo", REPO, {})).id)
    assert second.backup_id == first.backup_id  # no second identical backup
    _, total = await services.db.list_backups(limit=1)
    assert total == 1

    await services.safety.cancel(second.id)
    commit(work, "changed.txt", "x", "2025-05-01T00:00:00Z")
    git(work, "push", "-q", str(remotes / "proj.git"), "main")
    third = await services.safety.approve((await services.safety.start("delete_repo", REPO, {})).id)
    assert third.backup_id != first.backup_id  # GitHub changed: a fresh backup is taken
    assert (await services.db.get_backup(third.backup_id)).status == "verified"
