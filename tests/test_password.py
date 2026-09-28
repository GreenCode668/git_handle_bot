"""Password lock: setup, unlock, lockout, change, auto-lock and gate behaviour."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from telegram.ext import ApplicationHandlerStop

from ghbot.bot.handlers import password as pw
from ghbot.db import utcnow
from ghbot.services.auth import LOCKOUT, MAX_ATTEMPTS
from ghbot.validators import ValidationError
from tests.fakes import make_services

SECRET = "correct horse battery"
OTHER = "another-good-password"


@pytest.fixture
def env(tmp_path):
    services, gh, remotes = make_services(tmp_path)
    yield services
    services.db.close()


@pytest.fixture
def ui(monkeypatch):
    """Capture replies and message deletions instead of talking to Telegram."""
    sent: list[str] = []

    async def fake_reply(update, context, text, markup=None, *, edit=True):
        sent.append(text)

    async def fake_forget(update):
        sent.append("<deleted>")

    monkeypatch.setattr(pw, "reply", fake_reply)
    monkeypatch.setattr(pw, "_forget", fake_forget)
    return sent


def make_update(text: str | None = None, callback: bool = False):
    answered: list[str] = []

    async def answer(text="", show_alert=False):
        answered.append(text)

    query = SimpleNamespace(answer=answer, data=("noop",), message=None) if callback else None
    message = SimpleNamespace(text=text) if text is not None else None
    update = SimpleNamespace(callback_query=query, effective_message=message,
                             effective_chat=SimpleNamespace(id=1), effective_user=SimpleNamespace(id=1))
    return update, answered


def make_context(services):
    sent: list[str] = []

    async def send_message(chat_id, text, reply_markup=None):
        sent.append(text)

    return SimpleNamespace(user_data={}, application=SimpleNamespace(bot_data={"services": services}),
                           bot=SimpleNamespace(send_message=send_message)), sent


# ------------------------------------------------------------------ service
async def test_password_is_only_stored_hashed(env, tmp_path):
    await env.lock.set_password(SECRET)
    row = await env.db.get_auth()
    assert row["algorithm"] == "scrypt" and len(row["salt"]) == 16 and len(row["hash"]) == 32
    assert await env.lock.verify(SECRET) and not await env.lock.verify(SECRET + "x")
    env.db.close()  # flush WAL, then check the file itself never contains the password
    assert SECRET.encode() not in (tmp_path / "bot.db").read_bytes()


@pytest.mark.parametrize("bad", ["short", " leading", "x" * 129, "with\nnewline"])
async def test_weak_passwords_rejected(env, bad):
    with pytest.raises(ValidationError):
        await env.lock.set_password(bad)
    assert not await env.lock.is_configured()


async def test_unlock_wrong_then_right(env):
    await env.lock.set_password(SECRET)
    env.lock.lock()
    result = await env.lock.unlock("nope")
    assert not result.ok and result.attempts_left == MAX_ATTEMPTS - 1 and not env.lock.unlocked
    assert (await env.lock.unlock(SECRET)).ok and env.lock.unlocked
    assert (await env.db.get_auth())["failed_attempts"] == 0


async def test_lockout_after_repeated_failures(env):
    await env.lock.set_password(SECRET)
    env.lock.lock()
    for _ in range(MAX_ATTEMPTS):
        assert not (await env.lock.unlock("wrong")).ok
    blocked = await env.lock.unlock(SECRET)  # correct password, but locked out
    assert not blocked.ok and "Try again in" in blocked.message and not env.lock.unlocked
    assert await env.lock.lockout_remaining() <= LOCKOUT
    await env.db.set_auth_failures(0, utcnow() - timedelta(minutes=1))  # lockout expired
    assert (await env.lock.unlock(SECRET)).ok


async def test_change_password_requires_current(env):
    await env.lock.set_password(SECRET)
    with pytest.raises(ValidationError, match="not correct"):
        await env.lock.change_password("wrong", OTHER)
    with pytest.raises(ValidationError, match="different"):
        await env.lock.change_password(SECRET, SECRET)
    await env.lock.change_password(SECRET, OTHER)
    assert await env.lock.verify(OTHER) and not await env.lock.verify(SECRET)


async def test_autolock(env):
    await env.lock.set_password(SECRET)
    await env.db.set_setting("auth_autolock_minutes", 15)
    assert not await env.lock.check_autolock()
    env.lock._last_activity = utcnow() - timedelta(minutes=16)
    assert await env.lock.check_autolock() and not env.lock.unlocked


# --------------------------------------------------------------------- gate
async def test_gate_blocks_everything_until_unlocked(env, ui):
    await env.lock.set_password(SECRET)
    env.lock.lock()
    context, sent = make_context(env)

    update, _ = make_update("/repos")
    with pytest.raises(ApplicationHandlerStop):
        await pw.lock_gate(update, context)
    assert "locked" in ui[-1].lower()

    button, answered = make_update(callback=True)
    with pytest.raises(ApplicationHandlerStop):
        await pw.lock_gate(button, context)
    assert "locked" in answered[0].lower()

    wrong, _ = make_update("guess")
    with pytest.raises(ApplicationHandlerStop):
        await pw.lock_gate(wrong, context)
    assert "<deleted>" in ui and "Wrong password" in ui[-1]

    right, _ = make_update(SECRET)
    with pytest.raises(ApplicationHandlerStop):
        await pw.lock_gate(right, context)
    assert env.lock.unlocked and "Unlocked" in sent[-1]

    passes, _ = make_update("/repos")
    await pw.lock_gate(passes, context)  # no exception: normal handling continues


async def test_gate_setup_flow_when_no_password(env, ui):
    context, sent = make_context(env)
    update, _ = make_update("/start")
    with pytest.raises(ApplicationHandlerStop):
        await pw.lock_gate(update, context)
    assert "Set a password" in ui[-1]

    first, _ = make_update(SECRET)
    with pytest.raises(ApplicationHandlerStop):
        await pw.lock_gate(first, context)
    assert "once more" in ui[-1]

    mismatch, _ = make_update("different-password")
    with pytest.raises(ApplicationHandlerStop):
        await pw.lock_gate(mismatch, context)
    assert "did not match" in ui[-1] and not await env.lock.is_configured()

    for _ in range(2):
        again, _ = make_update(SECRET)
        with pytest.raises(ApplicationHandlerStop):
            await pw.lock_gate(again, context)
    assert await env.lock.is_configured() and env.lock.unlocked
    assert "Password set" in sent[-1]
