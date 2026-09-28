"""Start/help, status, history, settings, cancel, text routing and errors."""

from __future__ import annotations

import asyncio
import logging
import shutil

from telegram import Update
from telegram.ext import ApplicationHandlerStop

from ghbot import __version__
from ghbot.bot import keyboards
from ghbot.bot.handlers import password
from ghbot.bot.handlers.backups import show_backup_menu
from ghbot.bot.handlers.badges import show_badges
from ghbot.bot.handlers.dashboard import show_dashboard
from ghbot.bot.handlers.operations import STAGE_EMOJI, handle_confirmation_text
from ghbot.bot.handlers.profile import show_profile
from ghbot.bot.handlers import pullrequests
from ghbot.bot.handlers.repos import show_repo_list, try_repo_action_text
from ghbot.bot.router import CALLBACKS, INPUTS, callback, clear_input
from ghbot.bot.ui import Ctx, ago, btn, code, fmt_bytes, h, join_limited, kb, pager, reply, safe_error, svc
from ghbot.github.client import GitHubError
from ghbot.logging_setup import get_redactor
from ghbot.services.policy import PolicyError
from ghbot.services.safety import RUNNING, WAITING, SafetyError
from ghbot.validators import ValidationError

log = logging.getLogger(__name__)

HELP = """🤖 <b>GitHub manager</b> (private · public repositories only)

<b>Overview &amp; monitoring</b>
/dashboard · /status · /health · /ratelimit · /activity · /notifications
/history · /failed-ops · /locks

<b>Repositories</b>
/repos · /recent · /search text · /favorites
/repo name · /stats repo · /languages repo · /clone repo
/branches repo · /tags repo · /releases repo · /issues repo · /pulls repo
/create name · /import url
/years (commit years per repo) · /contributions (profile year tabs)
/description repo [text] · /homepage repo [url] · /topics repo
/favorite repo · /unfavorite repo
Quick: <code>repo @info | @stats | @commits | @branches | @actions | @backup</code>
<code>repo @rename new-name | @archive | @unarchive | @remove</code>

<b>Commits</b>
/commits [repo] [branch] · /latest repo · /commit repo sha
/commits-before repo YYYY-MM-DD · /commits-after repo YYYY-MM-DD
/compare repo base head · /contributors repo · /history-stats repo
/analyze repo YYYY-MM-DD: read-only analysis, then optional safe cleanup
/reauthor repo: map commit authors to your identity
/analyze-many [YYYY-MM-DD]: same for all or selected repositories

<b>Backups</b>
/backup [repo] · /backups [BK-id] · /backup-repo repo · /backup-diff BK-id
/backup-all · /verify-all · /backup-status · /autobackup
/restore BK-id [repo] · /undo

<b>GitHub Actions</b>
/actions repo · /workflows repo · /runs repo · /failed repo
/rerun repo run-id · /cancel-run repo run-id

<b>🔍 Code review &amp; fix</b>
/review repo [commits] [--force] · /review-issue repo 123 · /review-status

<b>🏆 PR automation</b>
/pr (menu) · /pr-create [repo] · /pr-list · /pr-status [id] · /pr-history
/pr-auto on|off (auto-merge) · /pr-settings · /achievements

<b>Profile</b>
/profile · /bio text · /name text · /location text · /website url
/socials · /social-add [url] · /social-remove
/aboutme (profile README) · /badges (README badge section)

<b>Safety</b>
/dryrun operation · /pending · /operation id · /cancel [id]
/lock repo · /unlock repo · /protected

/settings · /help · /guide (step-by-step help) · /password · /lockbot
Hyphenated commands also work with underscores (e.g. /backup_all).

📖 New here? Send /guide for step-by-step instructions for every action.

🛡 Only PUBLIC repositories owned by you are ever modified. Every write follows:
analyze → impact → approve → backup (when required) → verify → confirm → execute → verify → report."""


async def cmd_start(update: Update, context: Ctx) -> None:
    await update.effective_message.reply_text(HELP, reply_markup=keyboards.main_menu())  # type: ignore[union-attr]


async def cmd_help(update: Update, context: Ctx) -> None:
    await cmd_start(update, context)


async def cmd_cancel(update: Update, context: Ctx) -> None:
    if context.args:
        from ghbot.bot.handlers.safetycmds import cancel_operation_by_id

        await cancel_operation_by_id(update, context, context.args[0])
        return
    had_input = clear_input(context) is not None
    for key in ("import", "profile_pending", "social_pending", "status_pending", "badge_publish", "mbatch"):
        context.user_data.pop(key, None)  # type: ignore[union-attr]
    cancelled = await svc(context).safety.cancel_all_waiting()
    await reply(update, context, f"✖️ Cancelled. Pending operations cancelled: {cancelled}"
                                 f"{' · input cleared' if had_input else ''}. Nothing was changed.", edit=False)


async def cmd_status(update: Update, context: Ctx) -> None:
    s = svc(context)
    lines = [f"🩺 <b>Status</b> · bot v{__version__}", ""]
    try:
        user = await s.gh.get_user()
        rate = await s.gh.rate_limit()
        core = rate["resources"]["core"]
        lines.append(f"✅ GitHub API: authenticated as {code(user['login'])}")
        lines.append(f"   rate limit {core['remaining']}/{core['limit']}")
        scopes = s.gh.client.last_scopes
        lines.append(f"   token scopes: {h(scopes) if scopes is not None else 'fine-grained token (scopes not reported)'}")
        if scopes is not None:
            missing = [x for x in ("repo", "delete_repo", "user", "workflow") if x not in scopes]
            if missing:
                lines.append(f"   ⚠️ missing scopes: {h(', '.join(missing))}")
    except GitHubError as exc:
        lines.append(f"❌ GitHub API: {safe_error(exc)}")
    try:
        lines.append(f"✅ {h(await asyncio.to_thread(s.git.version))}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"❌ git: {safe_error(exc)}")
    usage = shutil.disk_usage(s.settings.backup_path)
    _, backups_total = await s.db.list_backups(limit=1)
    lines.append(f"💾 Backups: {backups_total} · free disk {fmt_bytes(usage.free)} of {fmt_bytes(usage.total)}")
    locks = await s.db.list_locks()
    lines.append(f"🔒 Repository locks: {len(locks)}")
    lines += [f"   • {code(l['repo_key'])} — {h(l['holder'])} since {ago(l['acquired_at'])}" for l in locks]
    pending = await s.db.operations_in_stages([*WAITING, *RUNNING])
    lines.append(f"⏳ Pending/running operations: {len(pending)}")
    rows = []
    for op in pending:
        lines.append(f"   {STAGE_EMOJI.get(op.stage, '•')} #{op.id} {h(op.kind)} {h(op.repo or '')} ({h(op.stage)})")
        rows.append([btn(f"#{op.id} {op.kind}", "op", op.id)] + ([btn("✖️ Cancel", "op_cancel", op.id)] if op.stage in WAITING else []))
    rows.append([btn("🔄 Refresh", "status"), btn("📖 Guide", "guide")])
    await reply(update, context, join_limited(lines), kb(rows))


@callback("status")
async def cb_status(update: Update, context: Ctx, data: tuple) -> None:
    await cmd_status(update, context)


async def show_history(update: Update, context: Ctx, page: int) -> None:
    per_page = 10
    ops, total = await svc(context).db.list_operations(limit=per_page, offset=(page - 1) * per_page)
    lines = [f"🧾 <b>Operation history</b> ({total})", ""]
    rows = []
    for op in ops:
        lines.append(f"{STAGE_EMOJI.get(op.stage, '•')} #{op.id} {h(op.kind)} {h(op.repo or '')} · {ago(op.created_at)}"
                     + (f"\n    {h((op.error or '')[:100])}" if op.error else ""))
        if op.impact:
            rows.append([btn(f"#{op.id} {op.kind}"[:40], "op", op.id)])
    if not ops:
        lines.append("No operations yet.")
    last = max(1, -(-total // per_page))
    rows.append(pager("history", page, page < last, last_page=last))
    await reply(update, context, join_limited(lines), kb(rows))


@callback("history")
async def cb_history(update: Update, context: Ctx, data: tuple) -> None:
    await show_history(update, context, int(data[1]))


async def cmd_history(update: Update, context: Ctx) -> None:
    await show_history(update, context, 1)


SETTINGS = {
    "page_size": ("Repositories per page", [5, 8, 10, 15], 8),
    "confirmation_ttl_minutes": ("Confirmation timeout (minutes)", [15, 30, 60, 120], 30),
    "backup_include_wiki": ("Include wiki in backups", [True, False], True),
}


async def show_settings(update: Update, context: Ctx) -> None:
    s = svc(context)
    lines = ["⚙️ <b>Settings</b>", ""]
    rows = []
    for key, (label, _choices, default) in SETTINGS.items():
        value = await s.db.get_setting(key, default)
        shown = ("yes" if value else "no") if isinstance(value, bool) else value
        lines.append(f"{h(label)}: <b>{h(shown)}</b>")
        rows.append([btn(f"🔁 {label}", "setting", key)])
    lines += ["", f"GitHub user: {code(s.username)}", f"Backup path: {code(s.settings.backup_path)}",
              f"Private repositories in read-only views: {'shown' if s.policy.show_private else 'hidden'} (SHOW_PRIVATE_REPOS)",
              "Write operations: public repositories only (not configurable)",
              "Tokens: configured (never displayed)"]
    rows += password.security_rows(await s.lock.autolock_minutes())
    rows += [
        [btn("🩺 Health", "health"), btn("⏱ Rate limit", "ratelimit"), btn("🧾 History", "history", 1)],
        [btn("⏳ Pending", "pending"), btn("❌ Failed ops", "failed_ops", 1), btn("🔒 Locks", "locks")],
        [btn("🛡 Protected", "protected"), btn("📰 Activity", "activity", 1), btn("🔔 Notifications", "notifications", 1)],
        [btn("🤖 Automatic backups", "autobackup"), btn("📖 Guide", "guide")],
    ]
    await reply(update, context, "\n".join(lines), kb(rows))


@callback("setting")
async def cb_setting(update: Update, context: Ctx, data: tuple) -> None:
    key = data[1]
    if key not in SETTINGS:
        return
    _, choices, default = SETTINGS[key]
    current = await svc(context).db.get_setting(key, default)
    index = choices.index(current) if current in choices else -1
    await svc(context).db.set_setting(key, choices[(index + 1) % len(choices)])
    await show_settings(update, context)


async def cmd_settings(update: Update, context: Ctx) -> None:
    await show_settings(update, context)


MENU_ACTIONS = {
    keyboards.DASHBOARD: lambda u, c: show_dashboard(u, c, 1),
    keyboards.REPOSITORIES: lambda u, c: show_repo_list(u, c, "detail", 1),
    keyboards.COMMITS: lambda u, c: show_repo_list(u, c, "commits", 1),
    keyboards.PROFILE: show_profile,
    keyboards.BADGES: show_badges,
    keyboards.BACKUPS: show_backup_menu,
    keyboards.PRS: pullrequests.show_menu,
    keyboards.SETTINGS: show_settings,
}


async def on_text(update: Update, context: Ctx) -> None:
    text = (update.effective_message.text or "").strip()  # type: ignore[union-attr]
    if text in MENU_ACTIONS:
        clear_input(context)
        await MENU_ACTIONS[text](update, context)
        return
    if await handle_confirmation_text(update, context, text):
        return
    state = context.user_data.get("awaiting")  # type: ignore[union-attr]
    if state and state.get("kind") in INPUTS:
        await INPUTS[state["kind"]](update, context, state, text)
        return
    if await try_repo_action_text(update, context, text):
        return
    await reply(update, context, "🤔 I didn't understand that. Use the menu or /help.", edit=False)


async def on_edited(update: Update, context: Ctx) -> None:
    await reply(update, context, "✏️ Edited messages are ignored for safety. Please send a new message.", edit=False)


async def on_callback(update: Update, context: Ctx) -> None:
    query = update.callback_query
    if query is None:
        return
    data = query.data
    await query.answer()
    if not isinstance(data, tuple) or not data or data[0] == "noop":
        return
    handler = CALLBACKS.get(data[0])
    if handler is None:
        return
    await handler(update, context, data)


async def on_invalid_callback(update: Update, context: Ctx) -> None:
    if update.callback_query:
        await update.callback_query.answer("This button expired. Please open the menu again.", show_alert=True)


async def on_error(update: object, context: Ctx) -> None:
    error = context.error
    if isinstance(update, Update) and isinstance(error, ValidationError | SafetyError | PolicyError):
        await reply(update, context, f"⚠️ {h(error)}", edit=False)
        return
    log.error("Unhandled error: %s", get_redactor()(repr(error)), exc_info=error)
    if isinstance(update, Update) and update.effective_chat:
        try:
            await context.bot.send_message(update.effective_chat.id, f"❌ Error: {safe_error(error or 'unknown')}")
        except Exception:  # noqa: BLE001
            pass


async def auth_guard(update: Update, context: Ctx) -> None:
    allowed = svc(context).settings.telegram_allowed_user_id
    user = update.effective_user
    chat = update.effective_chat
    if user is None or user.id != allowed or (chat is not None and chat.type != "private"):
        if user is not None:
            log.warning("Ignored update from unauthorized user id %s", user.id)
        if update.callback_query:
            try:
                await update.callback_query.answer()
            except Exception:  # noqa: BLE001
                pass
        raise ApplicationHandlerStop
