"""/achievements: factual contribution statistics.

GitHub exposes no API for achievement progress, so nothing here predicts or
promises a badge. Only real counts from the API are shown.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from telegram import Update

from ghbot.bot.router import callback
from ghbot.bot.ui import Ctx, ProgressMessage, btn, h, join_limited, kb, svc
from ghbot.github.client import GitHubError
from ghbot.services.contributions import year_contributions

ACHIEVEMENTS = [
    ("Pull Shark", "merged pull requests (2, 16, 128, 1024)"),
    ("Pair Extraordinaire", "co-authored commits in a merged pull request"),
    ("Quickdraw", "close an issue or pull request within 5 minutes"),
    ("YOLO", "merge a pull request without a review"),
    ("Starstruck", "a repository of yours reaches 16 stars"),
    ("Galaxy Brain", "an accepted answer in a GitHub Discussion"),
]


async def show_achievements(update: Update, context: Ctx) -> None:
    s = svc(context)
    progress = await ProgressMessage.create(update, context, "🏅 Reading your GitHub statistics…")
    user = await s.gh.get_user()
    login = user["login"]
    year = datetime.now(UTC).year

    async def count(query: str) -> str:
        try:
            return str(await s.gh.search_count(query))
        except GitHubError:
            return "?"

    opened, merged, reviewed, issues = await asyncio.gather(
        count(f"author:{login} type:pr"),
        count(f"author:{login} type:pr is:merged"),
        count(f"reviewed-by:{login} type:pr"),
        count(f"author:{login} type:issue"),
    )
    try:
        this_year = await year_contributions(s.gh, login, year)
        commits_line = (f"Commits this year: {this_year.commits} · pull requests: "
                        f"{this_year.pull_requests} · issues: {this_year.issues}")
    except GitHubError:
        commits_line = "Contribution counts unavailable with this token."

    _, bot_merged = await s.db.list_pull_requests(account=s.active, statuses=["merged"], limit=1)
    lines = [
        f"🏅 <b>GitHub statistics</b> · {h(login)}", "",
        f"Pull requests opened: <b>{h(opened)}</b>",
        f"Pull requests merged: <b>{h(merged)}</b>",
        f"Pull requests reviewed by you: <b>{h(reviewed)}</b>",
        f"Issues opened: <b>{h(issues)}</b>",
        commits_line,
        f"Public repositories: {user.get('public_repos', '?')} · followers: {user.get('followers', '?')}",
        f"Merged through this bot: {bot_merged}",
        "",
        "ℹ️ <b>GitHub does not expose achievement progress through its API</b>, so these are plain counts, "
        "not a prediction. No action here guarantees a badge.",
        "",
        "<b>How the common achievements are earned</b>",
    ]
    lines += [f"• <b>{h(name)}</b>: {h(how)}" for name, how in ACHIEVEMENTS]
    lines += ["", "They appear by themselves when you do real work; the bot never fabricates activity."]
    await progress.finish(context, update, join_limited(lines),
                          kb([[btn("🏆 PR Automation", "pr_menu"), btn("📊 Contributions", "contribs")]]))


async def cmd_achievements(update: Update, context: Ctx) -> None:
    await show_achievements(update, context)


@callback("achievements")
async def cb_achievements(update: Update, context: Ctx, data: tuple) -> None:
    await show_achievements(update, context)
