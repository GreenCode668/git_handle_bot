"""Profile README: template rendering, validation, publishing and revert."""

from __future__ import annotations

import pytest

from ghbot.github.client import GitHubError
from ghbot.services.badges import END, START, Badge
from ghbot.services.profile_readme import publish_readme, read_state, render_template, validate_readme
from ghbot.validators import ValidationError
from tests.fakes import make_services


@pytest.fixture
def env(tmp_path):
    services, gh, _ = make_services(tmp_path)
    yield services, gh
    services.db.close()


USER = {"login": "me", "name": "Colin Trator", "bio": "Software engineer", "company": "Canva",
        "location": "Vienna, Austria", "blog": "https://example.com"}


def test_template_uses_profile_data_and_badges():
    badges = [Badge(1, "tech", "Python", {"name": "Python", "logo": "python", "color": "3776AB"})]
    text = render_template("me", USER, badges, ["https://discord.com/users/1"])
    assert "Colin Trator" in text and "Software engineer" in text
    assert "🏢 Canva" in text and "📍 Vienna, Austria" in text
    assert "discord.com" in text
    assert START in text and END in text and "logo=python" in text  # badge section stays managed
    assert "github-readme-stats.vercel.app/api?username=me" in text


def test_template_without_optional_fields():
    text = render_template("me", {"login": "me"}, [], [])
    assert "me" in text and "Where to find me" not in text and START not in text


@pytest.mark.parametrize("bad", ["", "   \n  ", "x" * 200_001, "a\x00b"])
def test_validate_readme_rejects_bad_content(bad):
    with pytest.raises(ValidationError):
        validate_readme(bad)


def test_validate_readme_normalizes_line_endings():
    assert validate_readme("# Hi\r\nthere") == "# Hi\nthere\n"


async def test_publish_creates_snapshot_and_verifies(env):
    services, gh = env
    gh.add_repo("me")  # the profile repository
    state = await read_state(services, "me/me")
    assert state.exists and not state.has_readme

    ok, _ = await publish_readme(services, "me/me", "# First version", "msg")
    assert ok and gh.files[("me/me", "README.md")] == "# First version\n"
    assert await services.db.latest_readme_snapshot("me/me") is None  # nothing existed to snapshot

    ok, _ = await publish_readme(services, "me/me", "# Second version", "msg")
    assert ok
    snapshot = await services.db.latest_readme_snapshot("me/me")
    assert snapshot["content"] == "# First version\n"  # previous content kept for revert

    ok, _ = await publish_readme(services, "me/me", snapshot["content"], "revert")
    assert ok and gh.files[("me/me", "README.md")] == "# First version\n"


async def test_publish_refuses_when_repo_missing(env):
    services, _ = env
    with pytest.raises(ValidationError, match="does not exist"):
        await publish_readme(services, "me/me", "# Hi", "msg")


async def test_publish_detects_concurrent_change(env):
    services, gh = env
    gh.add_repo("me")
    await publish_readme(services, "me/me", "# One", "msg")
    original_get = gh.get_file

    async def stale(full_name, path):
        result = await original_get(full_name, path)
        return (result[0], "sha-stale") if result else None

    gh.get_file = stale
    with pytest.raises(GitHubError):
        await publish_readme(services, "me/me", "# Two", "msg")


# --------------------------------------------------- author identity after a rename
async def test_author_identity_follows_configured_username(env):
    from types import SimpleNamespace

    from ghbot.bot.handlers.reauthor import target_identity

    services, _ = env
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"services": services}))

    await services.db.set_setting(services.setting_key("author_email"), "colin@example.com")
    assert await target_identity(context) == ("me", "colin@example.com")

    # a name left over from an older username must not stick
    await services.db.set_setting(services.setting_key("author_name"), "old-name")
    assert await target_identity(context) == ("me", "colin@example.com")

    # a name you typed yourself is kept
    await services.db.set_setting(services.setting_key("author_name"), "Colin Trator")
    await services.db.set_setting(services.setting_key("author_name_custom"), True)
    assert await target_identity(context) == ("Colin Trator", "colin@example.com")

    # an email set before this feature existed still works
    await services.db.set_setting("author_email", "legacy@example.com")
    await services.db.set_setting(services.setting_key("author_email"), None)
    assert (await target_identity(context))[1] == "legacy@example.com"
