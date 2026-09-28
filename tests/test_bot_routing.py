"""Text routing: menu buttons, confirmation phrases, pending inputs, @actions."""

from types import SimpleNamespace

import pytest

from ghbot.bot.handlers import common


class Recorder:
    def __init__(self):
        self.calls = []

    def make(self, name, result=True):
        async def fn(*args, **kwargs):
            self.calls.append(name)
            return result
        return fn


def update_with(text):
    message = SimpleNamespace(text=text)
    return SimpleNamespace(effective_message=message, callback_query=None, effective_chat=SimpleNamespace(id=1))


@pytest.fixture
def ctx():
    return SimpleNamespace(user_data={}, application=SimpleNamespace(bot_data={}))


async def test_menu_button_clears_pending_input(monkeypatch, ctx):
    rec = Recorder()
    monkeypatch.setitem(common.MENU_ACTIONS, "📊 Dashboard", rec.make("dashboard"))
    ctx.user_data["awaiting"] = {"kind": "rename", "repo": "me/x"}
    await common.on_text(update_with("📊 Dashboard"), ctx)
    assert rec.calls == ["dashboard"] and "awaiting" not in ctx.user_data


async def test_confirmation_phrase_takes_priority(monkeypatch, ctx):
    rec = Recorder()
    monkeypatch.setattr(common, "handle_confirmation_text", rec.make("confirm", True))
    monkeypatch.setattr(common, "try_repo_action_text", rec.make("action", True))
    ctx.user_data["awaiting"] = {"kind": "rename"}
    await common.on_text(update_with("DELETE me/repo"), ctx)
    assert rec.calls == ["confirm"]


async def test_pending_input_then_repo_action(monkeypatch, ctx):
    rec = Recorder()
    monkeypatch.setattr(common, "handle_confirmation_text", rec.make("confirm", False))
    monkeypatch.setattr(common, "try_repo_action_text", rec.make("action", True))
    monkeypatch.setitem(common.INPUTS, "rename", rec.make("rename_input"))
    ctx.user_data["awaiting"] = {"kind": "rename"}
    await common.on_text(update_with("new-name"), ctx)
    ctx.user_data.clear()
    await common.on_text(update_with("repo @archive"), ctx)
    assert rec.calls == ["confirm", "rename_input", "confirm", "action"]


async def test_hyphen_commands_dispatch_and_never_fall_through(monkeypatch, ctx):
    from telegram.ext import ApplicationHandlerStop

    from ghbot.bot import app as app_module

    rec = Recorder()
    monkeypatch.setitem(app_module.COMMAND_HANDLERS, "backup_all", rec.make("backup_all"))
    monkeypatch.setitem(app_module.COMMAND_HANDLERS, "commits_before", rec.make("commits_before"))
    monkeypatch.setattr(app_module.common, "reply", rec.make("unknown"))
    for text in ("/backup-all", "/commits-before proj 2025-01-01", "/backup-foo proj"):
        with pytest.raises(ApplicationHandlerStop):
            await app_module.hyphen_command(update_with(text), ctx)
    assert rec.calls == ["backup_all", "commits_before", "unknown"]


async def test_analyze_date_input_accepts_repo_prefix_only_for_same_repo(monkeypatch):
    from ghbot.bot.handlers import repos
    from ghbot.validators import ValidationError

    started = []

    async def fake_start(update, context, kind, repo, params):
        started.append((repo.full_name, params["cutoff"]))

    monkeypatch.setattr(repos, "start_operation", fake_start)
    services = SimpleNamespace(username="trator0117", settings=SimpleNamespace(github_username="trator0117"))
    context = SimpleNamespace(user_data={}, application=SimpleNamespace(bot_data={"services": services}))
    state = {"kind": "analyze_date", "repo": "trator0117/cmap-resources"}
    for text in ("2013-1-1", "trator0117/cmap-resources 2013-04-13", "cmap-resources 2013-01-01"):
        await repos.input_analyze_date(update_with(text), context, state, text)
    assert started == [("trator0117/cmap-resources", "2013-01-01"), ("trator0117/cmap-resources", "2013-04-13"),
                       ("trator0117/cmap-resources", "2013-01-01")]
    with pytest.raises(ValidationError, match="You are analyzing"):
        await repos.input_analyze_date(update_with("other-repo 2013-01-01"), context, state, "other-repo 2013-01-01")
