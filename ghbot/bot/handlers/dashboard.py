"""Dashboard: account, repositories, commits, branches, Actions, recent operations."""

from __future__ import annotations

import asyncio
from typing import Any

from telegram import Update

from ghbot.bot.handlers.operations import STAGE_EMOJI
from ghbot.bot.router import callback
from ghbot.bot.ui import Ctx, ago, btn, code, h, join_limited, kb, pager, reply, run_emoji, svc
from ghbot.github.client import GitHubError

PER_PAGE = 4


async def _repo_summary(context: Ctx, repo: dict[str, Any]) -> list[str]:
    gh = svc(context).gh
    name = repo["full_name"]

    async def safe(coro):
        try:
            return await coro
        except GitHubError:
            return None

    commits, branches, runs = await asyncio.gather(
        safe(gh.list_commits(name, per_page=1)), safe(gh.list_branches(name, per_page=1)), safe(gh.list_runs(name, per_page=1))
    )
    flags = ("🔒" if repo["private"] else "🌍") + (" 📦" if repo["archived"] else "")
    lines = [f"{flags} <b>{h(repo['name'])}</b> · {code(repo.get('default_branch'))} · "
             f"🌿 {(branches.last_page or len(branches.items)) if branches else '?'}"]
    if commits and commits.items:
        c = commits.items[0]
        lines.append(f"   └ {code(c['sha'][:7])} {h(c['commit']['message'].splitlines()[0][:60])} · {ago(c['commit']['author']['date'])}")
    else:
        lines.append("   └ no commits")
    if runs and runs.items:
        r = runs.items[0]
        lines.append(f"   └ Actions: {run_emoji(r)} {h(r['name'])} ({ago(r['created_at'])})")
    return lines


async def show_dashboard(update: Update, context: Ctx, page: int = 1) -> None:
    s = svc(context)
    user, repos_page, ops = await asyncio.gather(
        s.gh.get_user(), s.gh.list_repos(page, PER_PAGE), s.db.list_operations(limit=5)
    )
    lines = [f"📊 <b>Dashboard</b> · {h(user['login'])}"]
    if page == 1:
        private = user.get("owned_private_repos") or user.get("total_private_repos") or 0
        lines += [
            f"👤 {h(user.get('name') or user['login'])} · followers {user['followers']} · following {user['following']}",
            f"📁 Repositories: {user['public_repos'] + private} (🌍 {user['public_repos']} · 🔒 {private})",
            f"⚡ API calls remaining: {s.gh.client.rate_remaining if s.gh.client.rate_remaining is not None else '?'}",
        ]
    lines += ["", f"<b>Recently pushed repositories</b> (page {page})"]
    summaries = await asyncio.gather(*(_repo_summary(context, r) for r in repos_page.items))
    for block in summaries:
        lines += block
    if not repos_page.items:
        lines.append("No repositories.")

    if page == 1:
        waiting = [o for o in (await s.db.operations_in_stages(["analyzed", "awaiting_confirmation", "backing_up", "executing"]))]
        lines += ["", "<b>Recent bot operations</b>"]
        operations, _ = ops
        lines += [f"{STAGE_EMOJI.get(o.stage, '•')} #{o.id} {h(o.kind)} {h(o.repo or '')} · {ago(o.created_at)}" for o in operations] or ["none"]
        if waiting:
            lines.append(f"⚠️ Pending/running operations: {len(waiting)} (see /status)")
    rows = [
        pager("dash", page, repos_page.has_next, last_page=repos_page.last_page),
        [btn("🔄 Refresh", "dash", page), btn("📁 Repositories", "repos", "detail", 1)],
        [btn("⚙️ Actions", "repos", "actions", 1), btn("🧾 History", "history", 1)],
    ]
    await reply(update, context, join_limited(lines), kb(rows))


@callback("dash")
async def cb_dash(update: Update, context: Ctx, data: tuple) -> None:
    await show_dashboard(update, context, int(data[1]))


async def cmd_dashboard(update: Update, context: Ctx) -> None:
    await show_dashboard(update, context, 1)
