"""Several GitHub accounts: configuration, switching and isolation."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from ghbot.config import ConfigError, load_settings
from ghbot.services.container import ACTIVE_ACCOUNT_SETTING
from ghbot.validators import ValidationError
from tests.fakes import make_services


@pytest.fixture
def env(tmp_path):
    services, gh, remotes = make_services(tmp_path, "me", extra=["second"])
    yield services, gh, remotes
    services.db.close()


def base_env(tmp_path, **extra):
    values = {
        "TELEGRAM_BOT_TOKEN": "123456789:" + "x" * 35,
        "TELEGRAM_ALLOWED_USER_ID": "42",
        "GITHUB_TOKEN": "ghp_" + "t" * 36,
        "GITHUB_USERNAME": "first-user",
        "BACKUP_PATH": str(tmp_path / "b"),
        "DATABASE_PATH": str(tmp_path / "db.sqlite"),
        **extra,
    }
    return values


def load(monkeypatch, tmp_path, **extra):
    for key in list(os.environ):
        if key.startswith(("GITHUB_", "TELEGRAM_", "BACKUP_", "DATABASE_", "WORK_", "SHOW_PRIVATE")):
            monkeypatch.delenv(key, raising=False)
    for key, value in base_env(tmp_path, **extra).items():
        monkeypatch.setenv(key, value)
    return load_settings(env_file=None)


def test_config_reads_numbered_accounts(monkeypatch, tmp_path):
    settings = load(monkeypatch, tmp_path, GITHUB_USERNAME_2="second-user", GITHUB_TOKEN_2="ghp_" + "s" * 36,
                    GITHUB_USERNAME_3="third-user", GITHUB_TOKEN_3="ghp_" + "u" * 36)
    assert [a.username for a in settings.accounts] == ["first-user", "second-user", "third-user"]
    assert len(settings.secrets) == 4  # telegram + three GitHub tokens, all redacted in logs
    assert settings.account("SECOND-USER").username == "second-user"
    assert settings.account("nobody") is None


@pytest.mark.parametrize("extra,message", [
    ({"GITHUB_USERNAME_2": "second-user"}, "must both be set"),
    ({"GITHUB_TOKEN_2": "ghp_" + "s" * 36}, "must both be set"),
    ({"GITHUB_USERNAME_2": "bad name", "GITHUB_TOKEN_2": "ghp_" + "s" * 36}, "not a valid GitHub username"),
    ({"GITHUB_USERNAME_2": "first-user", "GITHUB_TOKEN_2": "ghp_" + "s" * 36}, "Duplicate account"),
    ({"GITHUB_USERNAME_2": "second-user", "GITHUB_TOKEN_2": "short"}, "invalid format"),
])
def test_config_rejects_broken_accounts(monkeypatch, tmp_path, extra, message):
    with pytest.raises(ConfigError, match=message):
        load(monkeypatch, tmp_path, **extra)


async def test_switching_changes_active_account_and_persists(env):
    services, _, _ = env
    assert services.usernames == ["me", "second"] and services.active == "me"
    assert services.gh.username == "me" and services.policy.username == "me"

    await services.switch("SECOND")  # case-insensitive
    assert services.active == "second"
    assert services.gh.username == "second" and services.policy.username == "second"
    assert services.backups.gh is services.gh  # backups use the active account's client
    assert await services.db.get_setting(ACTIVE_ACCOUNT_SETTING) == "second"

    with pytest.raises(KeyError):
        await services.switch("nobody")

    services.active = "me"
    await services.load_active()  # what happens on restart
    assert services.active == "second"


async def test_per_account_settings_are_separate(env):
    services, _, _ = env
    await services.db.set_setting(services.setting_key("author_email"), "me@example.com")
    await services.switch("second")
    assert await services.db.get_setting(services.setting_key("author_email")) is None
    await services.db.set_setting(services.setting_key("author_email"), "second@example.com")
    await services.switch("me")
    assert await services.db.get_setting(services.setting_key("author_email")) == "me@example.com"


async def test_switch_is_blocked_while_operations_are_pending(env, tmp_path):
    import shutil

    from ghbot.bot.handlers.accounts import switch_to
    from ghbot.validators import RepoRef
    from tests.gitutil import make_sample_repo

    services, gh, remotes = env
    sample = tmp_path / "sample"
    sample.mkdir()
    _, remote = make_sample_repo(sample)
    shutil.move(str(remote), remotes / "proj.git")
    gh.add_repo("proj")

    sent = []

    async def fake_reply(update, context, text, markup=None, *, edit=True):
        sent.append(text)

    import ghbot.bot.handlers.accounts as accounts_module

    accounts_module.reply = fake_reply  # type: ignore[assignment]
    context = SimpleNamespace(user_data={}, application=SimpleNamespace(bot_data={"services": services}))
    update = SimpleNamespace(callback_query=None, effective_message=None, effective_chat=SimpleNamespace(id=1))

    op = await services.safety.start("delete_repo", RepoRef("me", "proj"), {})
    with pytest.raises(ValidationError, match="pending"):
        await switch_to(update, context, "second")
    assert services.active == "me"

    await services.safety.cancel(op.id)
    await switch_to(update, context, "second")
    assert services.active == "second" and "second" in sent[-1]


def test_config_reads_per_account_identities(monkeypatch, tmp_path):
    settings = load(monkeypatch, tmp_path,
                    GITHUB_EMAIL="first@example.com", GITHUB_NAME="First Person",
                    GITHUB_USERNAME_2="second-user", GITHUB_TOKEN_2="ghp_" + "s" * 36,
                    GITHUB_EMAIL_2="second@example.com")
    first, second = settings.accounts
    assert (first.email, first.display_name) == ("first@example.com", "First Person")
    assert (second.email, second.display_name) == ("second@example.com", None)


@pytest.mark.parametrize("extra,message", [
    ({"GITHUB_EMAIL": "not-an-email"}, "valid email"),
    ({"GITHUB_NAME": "a" * 81}, "invalid"),
    ({"GITHUB_USERNAME_2": "second-user", "GITHUB_TOKEN_2": "ghp_" + "s" * 36, "GITHUB_EMAIL_2": "nope"},
     "valid email"),
])
def test_config_rejects_broken_identities(monkeypatch, tmp_path, extra, message):
    with pytest.raises(ConfigError, match=message):
        load(monkeypatch, tmp_path, **extra)


async def test_identity_is_per_account_with_env_defaults(tmp_path):
    from dataclasses import replace

    from ghbot.bot.handlers.reauthor import identity_source, target_identity
    from ghbot.config import Account

    services, _, _ = make_services(tmp_path, "me", extra=["second"])
    try:
        services.settings = replace(
            services.settings,
            github_email="me@example.com",
            extra_accounts=(Account("second", "ghp_" + "e" * 36, "second@example.com", "Second Name"),),
        )
        context = SimpleNamespace(application=SimpleNamespace(bot_data={"services": services}))

        assert await target_identity(context) == ("me", "me@example.com")
        assert await identity_source(context) == "from .env"

        await services.switch("second")
        assert await target_identity(context) == ("Second Name", "second@example.com")

        # a value set in the bot wins over .env, for this account only
        await services.db.set_setting(services.setting_key("author_email"), "typed@example.com")
        assert (await target_identity(context))[1] == "typed@example.com"
        assert await identity_source(context) == "set in the bot for this account"
        await services.switch("me")
        assert (await target_identity(context))[1] == "me@example.com"
    finally:
        services.db.close()
