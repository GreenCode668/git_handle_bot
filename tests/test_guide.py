"""The in-bot guide: every topic renders, fits Telegram limits and links to real routes."""

from __future__ import annotations

import html
import re
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from ghbot.bot.guide import BY_KEY, TOPICS
from ghbot.bot.handlers import guide as guide_handler
from ghbot.config import Settings

TAG_RE = re.compile(r"</?([a-z]+)[^>]*>")
ALLOWED_TAGS = {"b", "i", "u", "s", "code", "pre", "a"}


@pytest.fixture(scope="module")
def routes():
    tmp = Path(tempfile.mkdtemp())
    (tmp / "b").mkdir()
    (tmp / "w").mkdir()
    from ghbot.bot.app import build_application

    build_application(Settings(telegram_bot_token="123456:" + "x" * 35, telegram_allowed_user_id=1,
                               github_token="ghp_" + "t" * 36, github_username="me", backup_path=tmp / "b",
                               database_path=tmp / "db", work_path=tmp / "w"))
    from ghbot.bot.router import CALLBACKS

    return set(CALLBACKS)


def test_topics_are_unique_and_named():
    assert len(BY_KEY) == len(TOPICS) >= 10
    for topic in TOPICS:
        assert topic.lines and topic.title.strip()


def test_topic_buttons_point_at_real_routes(routes):
    for topic in TOPICS:
        for label, route, *_ in topic.actions:
            assert label.strip()
            assert route in routes, f"{topic.key} -> {route}"


def test_topics_fit_in_one_telegram_message():
    for topic in TOPICS:
        rendered = "\n".join([topic.title, "", *topic.lines])
        assert len(rendered) < 3900, f"{topic.key} is {len(rendered)} chars"


def test_topics_use_only_supported_html():
    for topic in TOPICS:
        text = "\n".join(topic.lines)
        for tag in TAG_RE.findall(text):
            assert tag in ALLOWED_TAGS, f"{topic.key} uses <{tag}>"
        stripped = TAG_RE.sub("", text)
        # every remaining < > & must be an HTML entity, otherwise Telegram rejects the message
        assert "<" not in stripped and ">" not in stripped, f"{topic.key} has an unescaped angle bracket"
        for amp in re.findall(r"&[a-z]*;?", stripped):
            assert amp in ("&lt;", "&gt;", "&amp;"), f"{topic.key} has a bare '&'"


async def test_guide_menu_and_topics_render():
    sent = []

    async def fake_reply(update, context, text, markup=None, *, edit=True):
        sent.append((text, markup))

    guide_handler.reply = fake_reply  # type: ignore[assignment]
    context = SimpleNamespace(user_data={}, application=SimpleNamespace(bot_data={}))
    update = SimpleNamespace(callback_query=None, effective_message=None, effective_chat=SimpleNamespace(id=1))

    await guide_handler.show_menu(update, context)
    assert "Guide" in sent[-1][0] and len(sent[-1][1].inline_keyboard) == len(TOPICS)

    for topic in TOPICS:
        await guide_handler.show_topic(update, context, topic.key)
        text, markup = sent[-1]
        assert html.escape(topic.title, quote=False) in text
        assert any("All topics" in b.text for row in markup.inline_keyboard for b in row)

    await guide_handler.show_topic(update, context, "does-not-exist")
    assert "Guide" in sent[-1][0]
