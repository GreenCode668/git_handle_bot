from datetime import date

import pytest

from ghbot.validators import (
    ValidationError,
    parse_date,
    parse_repo,
    parse_repo_action,
    suggest_repo_name,
    validate_backup_id,
    validate_branch_name,
    validate_git_url,
    validate_repo_name,
)


@pytest.mark.parametrize("name", ["repo", "my-repo", "my_repo.v2", "A" * 100])
def test_valid_repo_names(name):
    assert validate_repo_name(name) == name


@pytest.mark.parametrize("name", ["", ".", "..", "a b", "a/b", "x.git", "A" * 101, "rm;-rf", "$(id)", "a\nb"])
def test_invalid_repo_names(name):
    with pytest.raises(ValidationError):
        validate_repo_name(name)


def test_parse_repo_restricts_owner():
    assert parse_repo("proj", "me").full_name == "me/proj"
    assert parse_repo("ME/proj", "me").full_name == "me/proj"
    with pytest.raises(ValidationError):
        parse_repo("someone/proj", "me")


@pytest.mark.parametrize("branch", ["main", "feature/x", "release-1.0"])
def test_valid_branches(branch):
    assert validate_branch_name(branch) == branch


@pytest.mark.parametrize("branch", ["", "-x", "a..b", "a b", "a~1", "a^", "a:b", "x.lock", "@", "a@{b", "/a", "a/", "a//b"])
def test_invalid_branches(branch):
    with pytest.raises(ValidationError):
        validate_branch_name(branch)


def test_parse_date():
    assert parse_date("2025-01-01", today=date(2026, 9, 15)) == date(2025, 1, 1)
    assert parse_date("2013-1-1", today=date(2026, 9, 15)) == date(2013, 1, 1)
    for bad in ("2025/01/01", "13-01-01", "2025-02-30", "2025-13-01", "1999-01-01", "2030-01-01", "yesterday", "2025-01-01; rm"):
        with pytest.raises(ValidationError):
            parse_date(bad, today=date(2026, 9, 15))


@pytest.mark.parametrize("url", [
    "https://github.com/owner/repo.git", "https://gitlab.com/group/sub/repo", "https://codeberg.org/a/b.git",
])
def test_valid_urls(url):
    assert validate_git_url(url) == url


@pytest.mark.parametrize("url", [
    "http://github.com/a/b", "git@github.com:a/b.git", "ssh://github.com/a/b", "file:///etc/passwd",
    "https://user:pass@github.com/a/b", "https://token@github.com/a/b", "https://127.0.0.1/a/b",
    "https://localhost/a/b", "https://[::1]/a/b", "https://github.com/a/b?x=1", "https://github.com/../b",
    "ext::sh -c id", "https://github.com/a b", "-uhttps://github.com/a/b", "https://github.com:8443/a/b",
    "https://10.0.0.5/a/b", "https://github.com/",
])
def test_invalid_urls(url):
    with pytest.raises(ValidationError):
        validate_git_url(url)


def test_suggest_repo_name():
    assert suggest_repo_name("https://github.com/o/My.Repo.git") == "My.Repo"
    assert suggest_repo_name("https://gitlab.com/g/some repo%20x") == "some-repo-20x"


def test_backup_id():
    assert validate_backup_id("bk-20260915-001") == "BK-20260915-001"
    for bad in ("BK-2026-001", "../BK-20260915-001", "BK-20260915-1"):
        with pytest.raises(ValidationError):
            validate_backup_id(bad)


def test_repo_action_parsing():
    a = parse_repo_action("my-repo @rename new-name")
    assert (a.repo, a.action, a.arg) == ("my-repo", "rename", "new-name")
    assert parse_repo_action("my-repo @delete").action == "remove"
    assert parse_repo_action("my-repo @info").action == "info"
    assert parse_repo_action("my-repo @explode") is None
    assert parse_repo_action("hello world") is None
    assert parse_repo_action("a; rm -rf / @remove") is None


def test_new_quick_actions_and_field_validators():
    from ghbot.validators import validate_homepage, validate_ref, validate_run_id, validate_sha, validate_topics

    for action in ("commits", "branches", "actions", "backup", "stats"):
        assert parse_repo_action(f"proj @{action}").action == action
    assert validate_topics(["Python", "bot", "python"]) == ["bot", "python"]
    for bad in (["-x"], ["a b"], ["x" * 51], [f"t{i}" for i in range(21)]):
        with pytest.raises(ValidationError):
            validate_topics(bad)
    assert validate_homepage("-") == ""
    with pytest.raises(ValidationError):
        validate_homepage("javascript:alert(1)")
    assert validate_ref("v1.0") == "v1.0" and validate_sha("abc123") == "abc123"
    with pytest.raises(ValidationError):
        validate_ref("main..evil")
    with pytest.raises(ValidationError):
        validate_run_id("12; rm")
