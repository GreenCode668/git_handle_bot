"""/guide: in-bot instructions for every action."""

from __future__ import annotations

from telegram import Update

from ghbot.bot.guide import BY_KEY, TOPICS
from ghbot.bot.router import callback
from ghbot.bot.ui import Ctx, btn, h, join_limited, kb, reply

MENU_INTRO = ("📖 <b>Guide</b>\nPick a topic. Each one explains what the action does and exactly how to run it.\n"
              "Full command list: /help")


def menu_keyboard():
    rows = [[btn(topic.title, "guide", topic.key)] for topic in TOPICS]
    return kb(rows)


async def show_menu(update: Update, context: Ctx) -> None:
    await reply(update, context, MENU_INTRO, menu_keyboard())


async def show_topic(update: Update, context: Ctx, key: str) -> None:
    topic = BY_KEY.get(key)
    if topic is None:
        await show_menu(update, context)
        return
    index = TOPICS.index(topic)
    rows = []
    jumps = [btn(label, *route) for label, *route in topic.actions]
    if jumps:
        rows.append(jumps)
    nav = []
    if index > 0:
        nav.append(btn("◀️ " + TOPICS[index - 1].title.split(" ", 1)[0], "guide", TOPICS[index - 1].key))
    nav.append(btn("📖 All topics", "guide"))
    if index < len(TOPICS) - 1:
        nav.append(btn(TOPICS[index + 1].title.split(" ", 1)[0] + " ▶️", "guide", TOPICS[index + 1].key))
    rows.append(nav)
    await reply(update, context, join_limited([f"<b>{h(topic.title)}</b>", "", *topic.lines]), kb(rows))


@callback("guide")
async def cb_guide(update: Update, context: Ctx, data: tuple) -> None:
    if len(data) > 1:
        await show_topic(update, context, data[1])
    else:
        await show_menu(update, context)


async def cmd_guide(update: Update, context: Ctx) -> None:
    if context.args:
        key = context.args[0].lower()
        if key in BY_KEY:
            await show_topic(update, context, key)
            return
    await show_menu(update, context)
