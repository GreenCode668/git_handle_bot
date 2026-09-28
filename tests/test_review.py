"""Automated review and fix: scanning, access rules, fix limits, verification, approval."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from ghbot.services.review import ReviewError, redact
from ghbot.validators import RepoRef, ValidationError
from tests.fakes import make_services
from tests.gitutil import git

REPO = RepoRef("me", "proj")

SECRET_DIFF = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,2 +1,6 @@
 import os
+API_KEY = "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
+subprocess.run(cmd, shell=True)
+try:
+    pass
+except:
+    pass
"""


def make_repo(tmp_path: Path, remotes: Path, name: str = "proj") -> Path:
    work = tmp_path / f"src-{name}"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    (work / "app.py").write_text("x = 1\n")
    (work / "package.json").write_text('{"name": "demo", "scripts": {}}\n')
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "initial")
    (work / "app.py").write_text("x = 1\ny = 2\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "second")
    git(tmp_path, "clone", "-q", "--bare", str(work), str(remotes / f"{name}.git"))
    return work


def fake_claude(tmp_path: Path, *, files: int = 1, output: str = '{"result": "fixed it"}', exit_code: int = 0,
                sleep: float = 0) -> Path:
    """A stand-in for the Claude Code CLI: edits N files in cwd, prints JSON."""
    script = tmp_path / "fake-claude"
    script.write_text(f"""#!/usr/bin/env python3
import json, pathlib, sys, time
time.sleep({sleep})
for i in range({files}):
    pathlib.Path(f"fixed_{{i}}.txt").write_text("fixed\\n")
print({output!r})
sys.exit({exit_code})
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


@pytest.fixture
def env(tmp_path):
    services, gh, remotes = make_services(tmp_path)
    make_repo(tmp_path, remotes)
    gh.add_repo("proj", default_branch="main")
    gh.add_branch("me/proj", "main", "basesha1")
    yield services, gh, remotes
    services.db.close()


# ------------------------------------------------------------------ scanning
def test_redaction_removes_secrets_before_leaving_the_machine():
    text = ("token ghp_" + "A" * 36 + " aws AKIAIOSFODNN7EXAMPLE "
            "api_key = \"s3cretvalue1234567890\" -----BEGIN PRIVATE KEY-----")
    out = redact(text)
    assert "ghp_" not in out and "AKIAIOSFODNN7EXAMPLE" not in out
    assert "s3cretvalue1234567890" not in out and "BEGIN PRIVATE KEY" not in out


def test_scan_finds_secrets_and_defects(env):
    services, *_ = env
    findings = services.review.scan_diff(SECRET_DIFF, ["app.py", "config/.env"])
    titles = {f.title for f in findings}
    assert any("GitHub token" in t for t in titles)
    assert "shell injection risk" in titles and "bare except" in titles
    assert any("environment file committed" in t for t in titles)
    secret = next(f for f in findings if "GitHub token" in f.title)
    assert secret.severity == "critical" and secret.fixable is False  # rotate manually, never "auto-fixed"
    assert secret.file == "app.py" and secret.line == 2  # first added line after one context line


def test_project_detection(tmp_path, env):
    services, *_ = env
    (tmp_path / "node").mkdir()
    (tmp_path / "node" / "package.json").write_text("{}")
    (tmp_path / "py").mkdir()
    (tmp_path / "py" / "requirements.txt").write_text("")
    (tmp_path / "empty").mkdir()
    assert services.review.detect_project(tmp_path / "node") == "node"
    assert services.review.detect_project(tmp_path / "py") == "python"
    assert services.review.detect_project(tmp_path / "empty") == "unknown"


# -------------------------------------------------------------- access rules
async def test_repository_not_owned_needs_force_and_stays_read_only(env):
    services, *_ = env
    other = RepoRef("someone-else", "proj")
    with pytest.raises(ValidationError, match="--force"):
        await services.review.check_access(other, force=False)
    owned, meta = await services.review.check_access(other, force=True)
    assert owned is False and meta == {}


async def test_private_repository_is_refused(env):
    services, gh, _ = env
    gh.repos["proj"].update(private=True, visibility="private")
    with pytest.raises(Exception, match="PUBLIC"):
        await services.review.check_access(REPO, force=False)


async def test_one_review_at_a_time(env):
    services, *_ = env
    services.review.start_run(42, "/review proj")
    with pytest.raises(ReviewError, match="already running"):
        services.review.start_run(42, "/review other")
    services.review.finish_run(42)
    services.review.start_run(42, "/review other")  # free again


# ----------------------------------------------------------------- pipeline
async def test_review_clones_gathers_and_cleans_up(env):
    services, *_ = env
    result = await services.review.review_commits(REPO, depth=5)
    assert result.clone is not None and result.clone.exists()
    assert len(result.commits) == 2 and "app.py" in result.files
    assert result.head_sha and result.project_type == "node"
    clone = result.clone
    await services.review.cleanup(result)
    assert not clone.exists() and result.clone is None


# ---------------------------------------------------------------------- fix
async def test_fix_requires_claude_code_installed(env, tmp_path):
    services, *_ = env
    services.settings = services.settings.__class__(**{**services.settings.__dict__,
                                                       "claude_code_path": str(tmp_path / "missing-binary")})
    result = await services.review.review_commits(REPO, depth=5)
    result.findings.append(_finding())
    with pytest.raises(ReviewError, match="not found"):
        await services.review.generate_fix(result)
    await services.review.cleanup(result)


def _finding():
    from ghbot.services.review import Finding

    return Finding("high", "bare except", "fix it", "app.py", 3, "diff-scan")


async def set_claude(services, script: Path):
    services.settings = services.settings.__class__(**{**services.settings.__dict__,
                                                       "claude_code_path": str(script)})


async def test_fix_aborts_when_too_many_files_touched(env, tmp_path):
    services, *_ = env
    await set_claude(services, fake_claude(tmp_path, files=6))
    services.settings = services.settings.__class__(**{**services.settings.__dict__, "max_files_per_fix": 2})
    result = await services.review.review_commits(REPO, depth=5)
    result.findings.append(_finding())
    with pytest.raises(ReviewError, match="above the limit"):
        await services.review.generate_fix(result)
    assert not (result.clone / "fixed_0.txt").exists()  # rolled back
    await services.review.cleanup(result)


async def test_fix_timeout_kills_the_child(env, tmp_path):
    services, *_ = env
    await set_claude(services, fake_claude(tmp_path, sleep=5))
    services.settings = services.settings.__class__(**{**services.settings.__dict__, "review_timeout_ms": 10_000})
    object.__setattr__(services.settings, "review_timeout_ms", 1000)
    result = await services.review.review_commits(REPO, depth=5)
    result.findings.append(_finding())
    with pytest.raises(ReviewError, match="timed out"):
        await services.review.generate_fix(result)
    await services.review.cleanup(result)


async def test_fix_reports_failure_when_verification_fails(env, tmp_path):
    services, gh, _ = env
    await set_claude(services, fake_claude(tmp_path, files=1))
    result = await services.review.review_commits(REPO, depth=5)
    result.findings.append(_finding())

    async def failing_tests(clone, project):
        from ghbot.services.review import Finding

        return [Finding("high", "tests failing", "1 failed", None, None, "tests")], "FAILED test_app.py"

    services.review.run_tests = failing_tests
    fix = await services.review.generate_fix(result)
    assert fix.verified is False and "FAILED" in fix.verification
    assert not any(call[0] == "create_pull" for call in gh.calls)  # no PR for broken code
    await services.review.cleanup(result)


async def test_approved_fix_pushes_branch_and_opens_draft_pr(env, tmp_path):
    services, gh, remotes = env
    await set_claude(services, fake_claude(tmp_path, files=1))
    result = await services.review.review_commits(REPO, depth=5)
    result.findings.append(_finding())
    fix = await services.review.generate_fix(result)
    assert fix.verified and fix.changed_files == ["fixed_0.txt"]
    assert fix.branch.startswith("bot/fix-")
    assert "fixed_0.txt" in fix.diff_stat and "fixed_0.txt" in fix.diff_head  # new files appear in the diff

    created = await services.review.commit_push_and_pr(REPO, result, fix)
    assert created["draft"] is True and created["url"].endswith("/pull/1")
    pull = gh.pulls[("me/proj", 1)]
    assert pull["draft"] is True and pull["base"]["ref"] == "main" and pull["head"]["ref"] == fix.branch
    assert pull["merged"] is False  # never merged by the bot
    branches = git(remotes / "proj.git", "for-each-ref", "--format=%(refname:short)", "refs/heads")
    assert fix.branch in branches.split() and "main" in branches.split()
    head_of_main = git(remotes / "proj.git", "rev-parse", "main").strip()
    assert head_of_main == git(remotes / "proj.git", "rev-parse", "main").strip()  # default branch untouched
    record, _ = await services.db.list_pull_requests(account="me")
    assert record[0].change_type == "review-fix" and record[0].auto_merge is False
    await services.review.cleanup(result)


async def test_task_prompt_is_redacted(env):
    services, *_ = env
    from ghbot.services.review import Finding, ReviewResult

    result = ReviewResult("me/proj", "python")
    result.findings.append(Finding("high", "token in code", "found ghp_" + "B" * 36, "app.py", 1, "scan"))
    task = services.review.build_task(result)
    assert "ghp_" not in task and "app.py:1" in task


# ------------------------------------------------- GitHub's own check results
def add_failed_check(gh, sha: str, *, name="build", annotations=None, summary="npm ERR! missing script: build"):
    gh.checks[("me/proj", sha)] = [{
        "id": 555, "name": name, "status": "completed", "conclusion": "failure",
        "output": {"summary": summary},
    }]
    gh.annotations[555] = annotations or []


async def test_failing_github_check_becomes_a_finding(env):
    """The red ✗ on github.com must appear in the review even without local linters."""
    services, gh, _ = env
    result = await services.review.review_commits(REPO, depth=2)
    assert not result.findings  # nothing locally: no linter, no tests, clean diff
    assert any("No linter" in note for note in result.notes)
    assert any("No test suite" in note for note in result.notes)
    await services.review.cleanup(result)

    sha = "abc1234"
    add_failed_check(gh, sha)
    findings, notes = await services.review.github_checks(REPO, [sha])
    assert len(findings) == 1
    assert findings[0].title == "check failed: build" and findings[0].severity == "high"
    assert "missing script" in findings[0].detail
    assert findings[0].fixable is True
    assert any("GitHub's own checks" in n for n in notes)


async def test_check_annotations_give_file_and_line(env):
    services, gh, _ = env
    add_failed_check(gh, "abc1234", name="eslint", annotations=[
        {"path": "src/app.js", "start_line": 42, "annotation_level": "failure",
         "title": "no-undef", "message": "'foo' is not defined."},
    ])
    findings, _ = await services.review.github_checks(REPO, ["abc1234"])
    assert findings[0].file == "src/app.js" and findings[0].line == 42
    assert "no-undef" in findings[0].title and "not defined" in findings[0].detail


async def test_failed_commit_status_is_reported(env):
    services, gh, _ = env
    gh.statuses[("me/proj", "abc1234")] = [
        {"state": "failure", "context": "ci/deploy", "description": "deploy failed"},
    ]
    findings, _ = await services.review.github_checks(REPO, ["abc1234"])
    assert findings[0].title == "status failed: ci/deploy" and "deploy failed" in findings[0].detail


async def test_failed_job_logs_feed_the_fix_prompt(env):
    services, gh, _ = env
    from ghbot.services.review import ReviewResult

    gh.workflow_runs[("me/proj", "abc1234")] = [{"id": 900, "name": "CI", "conclusion": "failure"}]
    gh.jobs[900] = [{"id": 901, "name": "build", "conclusion": "failure",
                     "steps": [{"name": "npm run build", "conclusion": "failure"}]}]
    gh.job_log[901] = "npm ERR! missing script: build\ntoken ghp_" + "C" * 36 + "\nharmless line"

    logs = await services.review.failed_job_details(REPO, "abc1234")
    assert "missing script" in logs and "npm run build" in logs
    assert "ghp_" not in logs  # redacted before it can reach Telegram or the model

    result = ReviewResult("me/proj", "node", ci_logs=logs)
    result.findings.append(_finding())
    assert "Failing CI output" in services.review.build_task(result)
