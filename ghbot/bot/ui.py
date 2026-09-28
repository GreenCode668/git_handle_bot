"""Formatting, pagination and message helpers for Telegram."""

from __future__ import annotations

import html
import logging
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from ghbot.logging_setup import get_redactor
from ghbot.services.container import Services

log = logging.getLogger(__name__)

LIMIT = 3900
Ctx = ContextTypes.DEFAULT_TYPE


def h(value: Any) -> str:
    """HTML-escape any value for Telegram HTML parse mode."""
    return html.escape(str(value), quote=False)


def code(value: Any) -> str:
    return f"<code>{h(value)}</code>"


def svc(context: Ctx) -> Services:
    return context.application.bot_data["services"]


def btn(text: str, *data: Any) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=tuple(data))


def url_btn(text: str, url: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, url=url)


def kb(rows: Iterable[Sequence[InlineKeyboardButton] | None]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([list(r) for r in rows if r])


def pager(route: str, page: int, has_next: bool, *extra: Any, last_page: int | None = None) -> list[InlineKeyboardButton] | None:
    row = []
    if page > 1:
        row.append(btn("◀️ Prev", route, *extra, page - 1))
    label = f"{page}/{last_page}" if last_page else f"page {page}"
    if page > 1 or has_next:
        row.append(btn(label, "noop"))
    if has_next:
        row.append(btn("Next ▶️", route, *extra, page + 1))
    return row or None


def join_limited(lines: Iterable[str], limit: int = LIMIT) -> str:
    out: list[str] = []
    size = 0
    for line in lines:
        if size + len(line) + 1 > limit:
            out.append("… <i>(truncated)</i>")
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out)


def ago(value: str | datetime | None) -> str:
    if not value:
        return "—"
    dt = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    return dt.strftime("%Y-%m-%d %H:%M")


def run_emoji(run: dict[str, Any]) -> str:
    if run.get("status") != "completed":
        return "⏳"
    return {"success": "✅", "failure": "❌", "cancelled": "🚫", "skipped": "⏭", "timed_out": "⌛"}.get(
        run.get("conclusion") or "", "⚪"
    )


def fmt_bytes(size: int | None) -> str:
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


async def reply(update: Update, context: Ctx, text: str, markup: Any = None, *, edit: bool = True) -> Message | None:
    """Edit the message behind a button press, or send a new message."""
    query = update.callback_query
    if query and edit and query.message:
        try:
            result = await query.edit_message_text(text, reply_markup=markup)
            return result if isinstance(result, Message) else None
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return None
            log.debug("edit failed, sending new message: %s", exc)
    chat_id = update.effective_chat.id if update.effective_chat else svc(context).settings.telegram_allowed_user_id
    return await context.bot.send_message(chat_id, text, reply_markup=markup)


class ProgressMessage:
    """A status message that is edited in place while a long task runs."""

    def __init__(self, message: Message | None) -> None:
        self.message = message

    @classmethod
    async def create(cls, update: Update, context: Ctx, text: str) -> ProgressMessage:
        return cls(await reply(update, context, text, edit=bool(update.callback_query)))

    async def __call__(self, text: str) -> None:
        if not self.message:
            return
        try:
            await self.message.edit_text(text)
        except BadRequest:
            pass

    async def finish(self, context: Ctx, update: Update, text: str, markup: Any = None) -> None:
        if self.message:
            try:
                await self.message.edit_text(text, reply_markup=markup)
                return
            except BadRequest:
                pass
        await reply(update, context, text, markup, edit=False)


def safe_error(exc: BaseException) -> str:
    return h(get_redactor()(str(exc)))[:800]
