"""Password gate: the bot starts locked and refuses everything until it is unlocked."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ApplicationHandlerStop

from ghbot.bot import keyboards
from ghbot.bot.router import await_input, callback, clear_input, text_input
from ghbot.bot.ui import Ctx, btn, h, reply, svc
from ghbot.services.auth import MIN_LENGTH, validate_password
from ghbot.validators import ValidationError

LOCKED_TEXT = "🔒 <b>The bot is locked.</b>\nSend your password to continue."
SETUP_TEXT = ("🔐 <b>Set a password</b>\nThe bot starts locked and asks for it after every restart.\n"
              f"Send a new password (at least {MIN_LENGTH} characters). It is stored only as a scrypt hash, "
              "and your message is deleted right after.")


async def _forget(update: Update) -> None:
    """Remove the message containing the password from the chat."""
    try:
        if update.effective_message:
            await update.effective_message.delete()
    except Exception:  # noqa: BLE001 - deletion is best effort
        pass


async def lock_gate(update: Update, context: Ctx) -> None:
    """Runs before every other handler. Raises ApplicationHandlerStop while locked."""
    lock = svc(context).lock
    configured = await lock.is_configured()
    if configured and await lock.check_autolock():
        await reply(update, context, "🔒 Locked again after inactivity.", edit=False)

    if configured and lock.unlocked:
        lock.touch()
        return  # normal processing continues

    if update.callback_query:
        await update.callback_query.answer("🔒 The bot is locked. Send your password.", show_alert=True)
        raise ApplicationHandlerStop

    message = update.effective_message
    text = (message.text or "").strip() if message else ""
    if not text:
        raise ApplicationHandlerStop

    if not configured:
        await _setup(update, context, text)
        raise ApplicationHandlerStop

    if text.startswith("/"):
        await reply(update, context, LOCKED_TEXT, edit=False)
        raise ApplicationHandlerStop

    await _attempt_unlock(update, context, text)
    raise ApplicationHandlerStop


async def _setup(update: Update, context: Ctx, text: str) -> None:
    lock = svc(context).lock
    state = context.user_data.get("pwsetup")  # type: ignore[union-attr]
    if text.startswith("/"):
        context.user_data.pop("pwsetup", None)  # type: ignore[union-attr]
        await reply(update, context, SETUP_TEXT, edit=False)
        return
    await _forget(update)
    if not state:
        try:
            validate_password(text)
        except ValidationError as exc:
            await reply(update, context, f"⚠️ {h(exc)}\n\n{SETUP_TEXT}", edit=False)
            return
        context.user_data["pwsetup"] = {"first": text}  # type: ignore[index]
        await reply(update, context, "🔐 Send the same password once more to confirm.", edit=False)
        return
    context.user_data.pop("pwsetup", None)  # type: ignore[union-attr]
    if text != state["first"]:
        await reply(update, context, f"⚠️ The passwords did not match.\n\n{SETUP_TEXT}", edit=False)
        return
    await lock.set_password(text)
    await context.bot.send_message(
        svc(context).settings.telegram_allowed_user_id,
        "✅ Password set and bot unlocked. It locks again whenever the bot restarts (/lockbot locks it now).",
        reply_markup=keyboards.main_menu(),
    )


async def _attempt_unlock(update: Update, context: Ctx, text: str) -> None:
    lock = svc(context).lock
    await _forget(update)
    result = await lock.unlock(text)
    if result.ok:
        await context.bot.send_message(
            svc(context).settings.telegram_allowed_user_id, "🔓 Unlocked. Welcome back.",
            reply_markup=keyboards.main_menu(),
        )
        return
    await reply(update, context, f"⛔ {h(result.message)}", edit=False)


# ----------------------------------------------------------- change password
async def cmd_password(update: Update, context: Ctx) -> None:
    await_input(context, "pw_current")
    await reply(update, context, "🔐 <b>Change password</b>\nSend your <b>current</b> password. /cancel to abort.", edit=False)


@callback("password")
async def cb_password(update: Update, context: Ctx, data: tuple) -> None:
    await cmd_password(update, context)


@text_input("pw_current")
async def input_current(update: Update, context: Ctx, state: dict, text: str) -> None:
    await _forget(update)
    if not await svc(context).lock.verify(text):
        clear_input(context)
        await reply(update, context, "⛔ That is not your current password. Nothing was changed.", edit=False)
        return
    await_input(context, "pw_new")
    await reply(update, context, f"🔐 Send the <b>new</b> password (at least {MIN_LENGTH} characters).", edit=False)


@text_input("pw_new")
async def input_new(update: Update, context: Ctx, state: dict, text: str) -> None:
    await _forget(update)
    try:
        validate_password(text)
    except ValidationError as exc:
        await reply(update, context, f"⚠️ {h(exc)} Send another password, or /cancel.", edit=False)
        return
    await_input(context, "pw_confirm", first=text)
    await reply(update, context, "🔐 Send the new password once more to confirm.", edit=False)


@text_input("pw_confirm")
async def input_confirm(update: Update, context: Ctx, state: dict, text: str) -> None:
    await _forget(update)
    clear_input(context)
    if text != state.get("first"):
        await reply(update, context, "⚠️ The passwords did not match. Nothing was changed. Start again with /password.", edit=False)
        return
    await svc(context).lock.set_password(text)
    await svc(context).db.log_simple("password_change", None, "done")
    await reply(update, context, "✅ Password changed. It is stored only as a scrypt hash.", edit=False)


async def cmd_lockbot(update: Update, context: Ctx) -> None:
    svc(context).lock.lock()
    clear_input(context)
    await reply(update, context, "🔒 Locked. Send your password to unlock.", edit=False)


@callback("lockbot")
async def cb_lockbot(update: Update, context: Ctx, data: tuple) -> None:
    await cmd_lockbot(update, context)


AUTOLOCK_CHOICES = [0, 15, 60, 240]


@callback("autolock")
async def cb_autolock(update: Update, context: Ctx, data: tuple) -> None:
    s = svc(context)
    current = await s.lock.autolock_minutes()
    index = AUTOLOCK_CHOICES.index(current) if current in AUTOLOCK_CHOICES else -1
    value = AUTOLOCK_CHOICES[(index + 1) % len(AUTOLOCK_CHOICES)]
    await s.db.set_setting("auth_autolock_minutes", value)
    from ghbot.bot.handlers.common import show_settings

    await show_settings(update, context)


def security_rows(minutes: int) -> list[list]:
    label = "off" if not minutes else f"{minutes} min"
    return [[btn("🔐 Change password", "password"), btn(f"⏱ Auto-lock: {label}", "autolock"), btn("🔒 Lock now", "lockbot")]]
