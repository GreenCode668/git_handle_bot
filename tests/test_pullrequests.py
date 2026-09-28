"""PR automation: creation, checks, reviews, protection, merging and limits."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from telegram.ext import ApplicationHandlerStop

from ghbot.services.pullrequests import validate_commit_message, validate_path
from ghbot.services.safety import SafetyError, Stage
from ghbot.validators import RepoRef, ValidationError
from tests.fakes import make_services

REPO = RepoRef("me", "proj")
BASE_PARAMS = {"change_kind": "readme", "path": "README.md", "text": "New documentation line."}


@pytest.fixture
def env(tmp_path):
    services, gh, remotes = make_services(tmp_path)
    gh.add_repo("proj", default_branch="main")
    gh.add_branch("me/proj", "main", "basesha1")
    gh.files[("me/proj", "README.md")] = "# proj\n"
    yield services, gh
    services.db.close()


async def open_pr(services, params=None, auto_merge=False):
    op = await services.safety.start("open_pr", REPO, {**BASE_PARAMS, **(params or {}), "auto_merge": auto_merge})
    assert op.stage == Stage.ANALYZED, op.error or op.impact.get("blockers")
    return await services.safety.confirm_button(op.id)


def green(gh, sha, name="build"):
    gh.set_checks("me/proj", sha, [{"name": name, "status": "completed", "conclusion": "success"}])


# ------------------------------------------------------------------ creation
async def test_pull_request_is_created_with_branch_and_commit(env):
    services, gh = env
    done = await open_pr(services)
    assert done.stage == Stage.DONE, done.error
    result = done.result
    assert result["number"] == 1 and result["branch"].startswith("bot/readme-")
    assert gh.files[("me/proj", "README.md")].endswith("New documentation line.\n")
    assert ("create_pull", "me/proj", 1) in gh.calls

    record, total = await services.db.list_pull_requests(account="me")
    assert total == 1 and record[0].status == "open" and record[0].number == 1
    assert record[0].operation_id == done.id  # linked to the operation history


async def test_change_builders_validate_input(env):
    services, gh = env
    gh.files[("me/proj", "app.py")] = "x = 1\ny = 2   \n"
    service = services.pulls

    change = await service.build_change(REPO, "whitespace", {"path": "app.py"})
    assert change.new_content == "x = 1\ny = 2\n" and change.stats[1] == 1

    with pytest.raises(ValidationError, match="does not appear"):
        await service.build_change(REPO, "replace", {"path": "app.py", "old_str": "nope", "new_str": "z"})
    gh.files[("me/proj", "dup.txt")] = "a\na\n"
    with pytest.raises(ValidationError, match="appears 2 times"):
        await service.build_change(REPO, "replace", {"path": "dup.txt", "old_str": "a", "new_str": "b"})
    with pytest.raises(ValidationError, match="not modify"):  # identical content is refused
        await service.build_change(REPO, "file", {"path": "app.py", "content": "x = 1\ny = 2   "})
    with pytest.raises(ValidationError):
        validate_path("../../etc/passwd")
    with pytest.raises(ValidationError):
        validate_commit_message("x")


async def test_private_repository_is_rejected(env):
    services, gh = env
    gh.repos["proj"].update(private=True, visibility="private")
    with pytest.raises(SafetyError, match="Only PUBLIC"):
        await services.safety.start("open_pr", REPO, BASE_PARAMS)
    assert not any(call[0] == "create_pull" for call in gh.calls)


# -------------------------------------------------------------------- merge
async def test_merge_after_successful_checks_and_branch_deletion(env):
    services, gh = env
    done = await open_pr(services)
    green(gh, done.result["head_sha"])
    record_id = done.result["record_id"]

    op = await services.safety.start("merge_pr", REPO, {"record_id": record_id})
    assert op.stage == Stage.ANALYZED, op.impact.get("blockers")
    op = await services.safety.approve(op.id)
    merged = await services.safety.confirm_phrase("MERGE me/proj")
    assert merged.stage == Stage.DONE, merged.error
    assert gh.pulls[("me/proj", 1)]["merged"] is True
    assert ("delete_ref", done.result["branch"]) in gh.calls
    record = await services.db.get_pull_request(record_id)
    assert record.status == "merged" and record.merged_at is not None


async def test_failing_checks_block_the_merge(env):
    services, gh = env
    done = await open_pr(services)
    gh.set_checks("me/proj", done.result["head_sha"],
                  [{"name": "tests", "status": "completed", "conclusion": "failure"}])
    op = await services.safety.start("merge_pr", REPO, {"record_id": done.result["record_id"]})
    assert op.stage == Stage.CANCELLED
    assert any("failing checks: tests" in b for b in op.impact["blockers"])
    assert gh.pulls[("me/proj", 1)]["merged"] is False


async def test_pending_checks_block_the_merge(env):
    services, gh = env
    done = await open_pr(services)
    gh.set_checks("me/proj", done.result["head_sha"], [{"name": "build", "status": "in_progress"}])
    status = await services.pulls.status(REPO, 1)
    assert "checks still running: build" in "; ".join(status.blockers(True))


async def test_merge_conflict_blocks_the_merge(env):
    services, gh = env
    done = await open_pr(services)
    green(gh, done.result["head_sha"])
    gh.pulls[("me/proj", 1)].update(mergeable=False, mergeable_state="dirty")
    op = await services.safety.start("merge_pr", REPO, {"record_id": done.result["record_id"]})
    assert op.stage == Stage.CANCELLED and any("conflict" in b for b in op.impact["blockers"])


async def test_protected_branch_required_checks_must_report(env):
    services, gh = env
    gh.protect("me/proj", "main", checks=["ci/build"])
    done = await open_pr(services)
    # nothing reported yet: the required check counts as pending, so no merge
    status = await services.pulls.status(REPO, 1)
    assert status.checks.pending == ["ci/build"]
    assert status.blockers(True)
    gh.set_checks("me/proj", done.result["head_sha"],
                  [{"name": "ci/build", "status": "completed", "conclusion": "success"}])
    status = await services.pulls.status(REPO, 1)
    assert status.checks.passed == ["ci/build"] and not status.blockers(True)


async def test_required_reviews_are_respected(env):
    services, gh = env
    gh.protect("me/proj", "main", reviews=1)
    done = await open_pr(services)
    green(gh, done.result["head_sha"])

    op = await services.safety.start("merge_pr", REPO, {"record_id": done.result["record_id"]})
    assert op.stage == Stage.CANCELLED
    assert any("0/1 required approvals" in b for b in op.impact["blockers"])
    assert not any(call[0] == "merge" for call in gh.calls)  # never merged without the approval

    gh.approve("me/proj", 1)
    op = await services.safety.start("merge_pr", REPO, {"record_id": done.result["record_id"]})
    assert op.stage == Stage.ANALYZED
    op = await services.safety.approve(op.id)
    assert (await services.safety.confirm_phrase("MERGE me/proj")).stage == Stage.DONE


async def test_changes_requested_blocks_merge(env):
    services, gh = env
    done = await open_pr(services)
    green(gh, done.result["head_sha"])
    gh.approve("me/proj", 1, user="critic", state="CHANGES_REQUESTED")
    status = await services.pulls.status(REPO, 1)
    assert "1 review(s) requested changes" in "; ".join(status.blockers(True))


# --------------------------------------------------------------- auto-merge
async def test_auto_merge_only_runs_when_enabled_and_green(env):
    services, gh = env
    done = await open_pr(services, auto_merge=False)
    green(gh, done.result["head_sha"])
    assert await services.pulls.auto_merge_round() == []  # disabled: untouched
    assert gh.pulls[("me/proj", 1)]["merged"] is False

    await services.db.update_pull_request(done.result["record_id"], auto_merge=True)
    gh.set_checks("me/proj", done.result["head_sha"],
                  [{"name": "build", "status": "completed", "conclusion": "failure"}])
    assert await services.pulls.auto_merge_round() == []  # enabled but red: still untouched

    green(gh, done.result["head_sha"])
    notes: list[str] = []
    merged = await services.pulls.auto_merge_round(notify=lambda text: notes.append(text) or _noop())
    assert merged == ["me/proj#1"] and gh.pulls[("me/proj", 1)]["merged"] is True
    assert notes and "Auto-merged" in notes[0]
    ops, _ = await services.db.list_operations(limit=5)
    assert any(o.kind == "pr_auto_merge" for o in ops)  # recorded in the operation history


async def _noop():
    return None


# ------------------------------------------------------------------- limits
async def test_concurrency_limit_and_repository_lock(env):
    services, gh = env
    await services.pulls.set_setting("pr_max_concurrent", 1)
    await open_pr(services)

    op = await services.safety.start("open_pr", REPO, BASE_PARAMS)
    assert op.stage == Stage.CANCELLED and any("already active" in b for b in op.impact["blockers"])

    await services.pulls.set_setting("pr_max_concurrent", 5)
    first = await services.safety.start("open_pr", REPO, BASE_PARAMS)
    second = await services.safety.start("open_pr", REPO, BASE_PARAMS)
    await services.db.acquire_lock(REPO.key, "other op", 999)
    with pytest.raises(SafetyError, match="locked"):
        await services.safety.confirm_button(first.id)
    await services.db.release_lock(REPO.key, 999)
    await services.safety.cancel(second.id)


async def test_allowed_repository_list(env):
    services, gh = env
    await services.pulls.set_setting("pr_allowed_repos", ["me/other"])
    op = await services.safety.start("open_pr", REPO, BASE_PARAMS)
    assert op.stage == Stage.CANCELLED and any("allowed list" in b for b in op.impact["blockers"])
    await services.pulls.set_setting("pr_allowed_repos", [])
    assert (await services.safety.start("open_pr", REPO, BASE_PARAMS)).stage == Stage.ANALYZED


async def test_settings_defaults_and_toggles(env):
    services, _ = env
    settings = await services.pulls.settings()
    assert settings["pr_auto_merge"] is False and settings["pr_merge_method"] == "squash"
    assert settings["pr_require_checks"] is True and settings["pr_max_concurrent"] == 3
    await services.pulls.set_setting("pr_merge_method", "rebase")
    assert (await services.pulls.settings())["pr_merge_method"] == "rebase"
    with pytest.raises(ValidationError):
        await services.pulls.set_setting("nonsense", 1)


async def test_unauthorized_telegram_user_cannot_use_pr_commands(env):
    from ghbot.bot.handlers.common import auth_guard

    services, _ = env
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"services": services}))
    intruder = SimpleNamespace(effective_user=SimpleNamespace(id=999),
                               effective_chat=SimpleNamespace(type="private"), callback_query=None)
    with pytest.raises(ApplicationHandlerStop):
        await auth_guard(intruder, context)


@pytest.mark.parametrize("bad", ["../../etc/passwd", "/etc/passwd", "./../x", "a//b", ".git/config", "dir/", ""])
def test_paths_outside_the_repository_are_rejected(bad):
    with pytest.raises(ValidationError):
        validate_path(bad)


@pytest.mark.parametrize("good,expected", [("README.md", "README.md"), ("./src/main.py", "src/main.py")])
def test_valid_paths(good, expected):
    assert validate_path(good) == expected
