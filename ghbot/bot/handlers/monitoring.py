"""Monitoring: health, rate limits, failed operations, locks, activity, notifications."""

from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import time

from telegram import Update

from ghbot import __version__
from ghbot.bot.handlers.operations import STAGE_EMOJI
from ghbot.bot.router import callback
from ghbot.bot.ui import Ctx, ago, btn, code, fmt_bytes, h, join_limited, kb, pager, reply, safe_error, svc
from ghbot.github.client import GitHubError
from ghbot.services.safety import Stage


def _ok(flag: bool) -> str:
    return "✅" if flag else "❌"


async def cmd_health(update: Update, context: Ctx) -> None:
    s = svc(context)
    lines = [f"🩺 <b>Health check</b> · bot v{__version__}", ""]

    started = time.monotonic()
    try:
        user = await s.gh.get_user()
        lines.append(f"✅ GitHub API: authenticated as {code(user['login'])} ({int((time.monotonic() - started) * 1000)} ms)")
        if user["login"].lower() != s.username.lower():
            lines.append("   ⚠️ token user differs from GITHUB_USERNAME")
    except GitHubError as exc:
        lines.append(f"❌ GitHub API: {safe_error(exc)}")

    try:
        me = await context.bot.get_me()
        lines.append(f"✅ Telegram bot: @{h(me.username)}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"❌ Telegram bot: {safe_error(exc)}")

    try:
        result = await s.db.quick_check()
        lines.append(f"{_ok(result == 'ok')} Database: integrity {h(result)} · {code(s.settings.database_path.name)}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"❌ Database: {safe_error(exc)}")

    try:
        lines.append(f"✅ Git: {h(await asyncio.to_thread(s.git.version))}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"❌ Git: {safe_error(exc)}")

    probe = s.settings.backup_path / f".health-{secrets.token_hex(4)}"
    try:
        probe.write_text("ok")
        writable = probe.read_text() == "ok"
        probe.unlink()
    except OSError:
        writable = False
    lines.append(f"{_ok(writable)} Backup storage writable: {code(s.settings.backup_path)}")
    work_ok = os.access(s.settings.work_path, os.W_OK)
    lines.append(f"{_ok(work_ok)} Work directory writable: {code(s.settings.work_path)}")

    usage = shutil.disk_usage(s.settings.backup_path)
    low = usage.free < 5 * 1024**3
    lines.append(f"{'⚠️' if low else '✅'} Disk space: {fmt_bytes(usage.free)} free of {fmt_bytes(usage.total)}")
    locks = await s.db.list_locks()
    pending = await s.db.operations_in_stages([Stage.ANALYZED, Stage.AWAITING_CONFIRMATION, Stage.BACKING_UP, Stage.EXECUTING])
    lines.append(f"ℹ️ Locks: {len(locks)} · pending/running operations: {len(pending)} · "
                 f"private repos visible: {'yes' if s.policy.show_private else 'no'}")
    await reply(update, context, "\n".join(lines), kb([[btn("🔄 Refresh", "health")]]))


@callback("health")
async def cb_health(update: Update, context: Ctx, data: tuple) -> None:
    await cmd_health(update, context)


async def cmd_ratelimit(update: Update, context: Ctx) -> None:
    data = await svc(context).gh.rate_limit()
    lines = ["⏱ <b>GitHub API rate limits</b>", ""]
    for name in ("core", "search", "graphql", "code_search"):
        r = data["resources"].get(name)
        if not r:
            continue
        pct = r["remaining"] * 100 // max(r["limit"], 1)
        reset_in = max(0, r["reset"] - int(time.time())) // 60
        lines.append(f"{'⚠️' if pct < 10 else '✅'} {h(name)}: {r['remaining']}/{r['limit']} ({pct}%) · resets in {reset_in} min")
    await reply(update, context, "\n".join(lines), kb([[btn("🔄 Refresh", "ratelimit")]]))


@callback("ratelimit")
async def cb_ratelimit(update: Update, context: Ctx, data: tuple) -> None:
    await cmd_ratelimit(update, context)


async def show_failed_ops(update: Update, context: Ctx, page: int) -> None:
    per_page = 10
    ops, total = await svc(context).db.list_operations(limit=per_page, offset=(page - 1) * per_page,
                                                        stages=[Stage.FAILED, Stage.INTERRUPTED])
    lines = [f"❌ <b>Failed operations</b> ({total})", ""]
    rows = []
    for op in ops:
        lines.append(f"{STAGE_EMOJI.get(op.stage, '•')} #{op.id} {h(op.kind)} {h(op.repo or '')} · {ago(op.updated_at)}\n"
                     f"    {h((op.error or '')[:160])}")
        rows.append([btn(f"#{op.id} {op.kind}"[:40], "op", op.id)])
    if not ops:
        lines.append("No failed operations. 🎉")
    last = max(1, -(-total // per_page))
    rows.append(pager("failed_ops", page, page < last, last_page=last))
    await reply(update, context, join_limited(lines), kb(rows))


async def cmd_failed_ops(update: Update, context: Ctx) -> None:
    await show_failed_ops(update, context, 1)


@callback("failed_ops")
async def cb_failed_ops(update: Update, context: Ctx, data: tuple) -> None:
    await show_failed_ops(update, context, int(data[1]))


async def cmd_locks(update: Update, context: Ctx) -> None:
    s = svc(context)
    locks = await s.db.list_locks()
    lines = ["🔒 <b>Repositories locked by an operation</b>", ""]
    rows = []
    for lock in locks:
        lines.append(f"• {code(lock['repo_key'])} — {h(lock['holder'])} since {ago(lock['acquired_at'])}")
        if lock.get("operation_id"):
            rows.append([btn(f"#{lock['operation_id']} details", "op", lock["operation_id"])])
    if not locks:
        lines.append("No operation locks.")
    protected = await s.db.list_protected()
    lines.append(f"\n🛡 Manually protected repositories: {len(protected)} (see /protected)")
    rows.append([btn("🔄 Refresh", "locks"), btn("🛡 Protected", "protected")])
    await reply(update, context, "\n".join(lines), kb(rows))


@callback("locks")
async def cb_locks(update: Update, context: Ctx, data: tuple) -> None:
    await cmd_locks(update, context)


EVENT_TEXT = {
    "PushEvent": lambda p: f"pushed {p.get('size', len(p.get('commits', [])))} commit(s) to {p.get('ref', '').removeprefix('refs/heads/')}",
    "CreateEvent": lambda p: f"created {p.get('ref_type')} {p.get('ref') or ''}",
    "DeleteEvent": lambda p: f"deleted {p.get('ref_type')} {p.get('ref')}",
    "PullRequestEvent": lambda p: f"{p.get('action')} PR #{p.get('number')}",
    "IssuesEvent": lambda p: f"{p.get('action')} issue #{(p.get('issue') or {}).get('number')}",
    "IssueCommentEvent": lambda p: f"commented on #{(p.get('issue') or {}).get('number')}",
    "ReleaseEvent": lambda p: f"{p.get('action')} release {(p.get('release') or {}).get('tag_name')}",
    "WatchEvent": lambda p: "starred",
    "ForkEvent": lambda p: "forked",
    "PublicEvent": lambda p: "made public",
}


async def show_activity(update: Update, context: Ctx, page: int) -> None:
    s = svc(context)
    result = await s.gh.user_events(page=page, per_page=20)
    lines = [f"📰 <b>Recent activity</b> · {h(s.username)}", ""]
    shown = 0
    for event in result.items:
        if not event.get("public", True) and not s.policy.show_private:
            continue  # private repository events stay hidden
        render = EVENT_TEXT.get(event["type"])
        text = render(event.get("payload") or {}) if render else event["type"].removesuffix("Event")
        lines.append(f"{ago(event['created_at'])} · <b>{h(event['repo']['name'].split('/', 1)[-1])}</b> — {h(text)}")
        shown += 1
    if not shown:
        lines.append("No recent visible activity.")
    await reply(update, context, join_limited(lines), kb([pager("activity", page, result.has_next and page < 10)]))


async def cmd_activity(update: Update, context: Ctx) -> None:
    await show_activity(update, context, 1)


@callback("activity")
async def cb_activity(update: Update, context: Ctx, data: tuple) -> None:
    await show_activity(update, context, int(data[1]))


async def show_notifications(update: Update, context: Ctx, page: int) -> None:
    s = svc(context)
    try:
        result = await s.gh.notifications(page=page, per_page=15)
    except GitHubError as exc:
        if exc.status in (401, 403, 404):
            await reply(update, context, "🔔 Notifications are not available with this token. GitHub only supports the "
                                         "notifications API for classic personal access tokens with the "
                                         "<code>notifications</code> or <code>repo</code> scope.")
            return
        raise
    lines = ["🔔 <b>Unread notifications</b>", ""]
    icons = {"PullRequest": "🔃", "Issue": "🐞", "Release": "📦", "CheckSuite": "⚙️", "Discussion": "💬", "Commit": "🔀"}
    shown = 0
    for n in result.items:
        repo = n.get("repository") or {}
        if repo.get("private") and not s.policy.show_private:
            continue
        subject = n.get("subject") or {}
        lines.append(f"{icons.get(subject.get('type'), '•')} <b>{h(repo.get('full_name', ''))}</b> — {h((subject.get('title') or '')[:90])}\n"
                     f"    {h(n.get('reason', ''))} · {ago(n.get('updated_at'))}")
        shown += 1
    if not shown:
        lines.append("No unread notifications.")
    await reply(update, context, join_limited(lines), kb([pager("notifications", page, result.has_next)]))


async def cmd_notifications(update: Update, context: Ctx) -> None:
    await show_notifications(update, context, 1)


@callback("notifications")
async def cb_notifications(update: Update, context: Ctx, data: tuple) -> None:
    await show_notifications(update, context, int(data[1]))
