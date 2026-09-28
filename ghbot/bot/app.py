"""Application wiring."""

from __future__ import annotations

import logging
import re
from datetime import timedelta

from telegram import BotCommand, LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    Defaults,
    InvalidCallbackData,
    MessageHandler,
    TypeHandler,
    filters,
)

from ghbot.bot.handlers import (
    aboutme,
    accounts,
    achievements,
    actions,
    backups,
    badges,
    commits,
    common,
    contributions,
    dashboard,
    guide,
    monitoring,
    multianalyze,
    password,
    profile,
    pullrequests,
    reauthor,
    repoinfo,
    review,
    repos,
    safetycmds,
)
from ghbot.bot.handlers import operations as _operations  # noqa: F401 - registers callbacks
from ghbot.bot.ui import Ctx, code, h
from ghbot.config import Settings
from ghbot.services.container import Services

log = logging.getLogger(__name__)

# (name, description, handler). Telegram command names only allow [a-z0-9_]; the hyphenated
# spellings from the help text are accepted through HYPHEN_ALIASES below.
COMMANDS: list[tuple[str, str, object]] = [
    ("start", "Show menu", common.cmd_start),
    ("help", "Help", common.cmd_help),
    ("guide", "Step-by-step guide for every action", guide.cmd_guide),
    ("dashboard", "Account overview", dashboard.cmd_dashboard),
    ("accounts", "GitHub accounts", accounts.cmd_accounts),
    ("switch", "Switch the active account", accounts.cmd_switch),
    ("status", "Bot status", common.cmd_status),
    ("health", "Health check", monitoring.cmd_health),
    ("ratelimit", "GitHub API rate limits", monitoring.cmd_ratelimit),
    ("activity", "Recent GitHub activity", monitoring.cmd_activity),
    ("notifications", "GitHub notifications", monitoring.cmd_notifications),
    ("history", "Operation history", common.cmd_history),
    ("failed_ops", "Failed bot operations", monitoring.cmd_failed_ops),
    ("locks", "Repositories locked by operations", monitoring.cmd_locks),
    ("repos", "List repositories", repos.cmd_repos),
    ("recent", "Recently updated repositories", repoinfo.cmd_recent),
    ("search", "Search repositories", repoinfo.cmd_search),
    ("favorites", "Favorite repositories", repoinfo.cmd_favorites),
    ("favorite", "Add a favorite", repoinfo.cmd_favorite),
    ("unfavorite", "Remove a favorite", repoinfo.cmd_unfavorite),
    ("repo", "Repository details/actions", repos.cmd_repo),
    ("stats", "Repository statistics", repoinfo.cmd_stats),
    ("languages", "Repository languages", repoinfo.cmd_languages),
    ("years", "Commit years per repository", repoinfo.cmd_years),
    ("contributions", "Contributions per year (profile year tabs)", contributions.cmd_contributions),
    ("clone", "Clone URL", repoinfo.cmd_clone),
    ("branches", "Branches", commits.cmd_branches),
    ("tags", "Tags", repoinfo.cmd_tags),
    ("releases", "Releases", repoinfo.cmd_releases),
    ("issues", "Open issues", repoinfo.cmd_issues),
    ("pulls", "Open pull requests", repoinfo.cmd_pulls),
    ("create", "Create a public repository", repoinfo.cmd_create),
    ("import", "Import a git repository", repos.cmd_import),
    ("description", "View/change description", repoinfo.cmd_description),
    ("homepage", "View/change homepage", repoinfo.cmd_homepage),
    ("topics", "View/change topics", repoinfo.cmd_topics),
    ("commits", "Browse commits", commits.cmd_commits),
    ("latest", "Latest commits", commits.cmd_latest),
    ("commit", "Commit details", commits.cmd_commit),
    ("commits_before", "Commits before a date", commits.cmd_commits_before),
    ("commits_after", "Commits after a date", commits.cmd_commits_after),
    ("compare", "Compare refs", commits.cmd_compare),
    ("contributors", "Contributors", commits.cmd_contributors),
    ("history_stats", "Commit history statistics", commits.cmd_history_stats),
    ("analyze", "Analyze history before a date", commits.cmd_analyze),
    ("reauthor", "Rewrite commit authors to your identity", reauthor.cmd_reauthor),
    ("identity", "Commit identity of the active account", reauthor.cmd_identity),
    ("analyze_many", "Analyze old commits in many repos", multianalyze.cmd_analyze_many),
    ("backup", "Create a backup", backups.cmd_backup),
    ("backups", "List backups", backups.cmd_backups),
    ("backup_repo", "Backups of a repository", backups.cmd_backup_repo),
    ("backup_diff", "Compare backup with GitHub", backups.cmd_backup_diff),
    ("backup_all", "Back up all public repositories", backups.cmd_backup_all),
    ("verify_all", "Verify all backups", backups.cmd_verify_all),
    ("backup_status", "Backup storage status", backups.cmd_backup_status),
    ("autobackup", "Automatic backup settings", backups.cmd_autobackup),
    ("restore", "Restore a backup", backups.cmd_restore),
    ("undo", "Undo last destructive operation", backups.cmd_undo),
    ("review", "Review recent commits and offer a fix", review.cmd_review),
    ("review_issue", "Review an open issue and offer a fix", review.cmd_review_issue),
    ("review_status", "Review engine status", review.cmd_review_status),
    ("pr", "PR automation menu", pullrequests.cmd_pr),
    ("pr_create", "Open a pull request", pullrequests.cmd_pr_create),
    ("pr_auto", "Toggle auto-merge", pullrequests.cmd_pr_auto),
    ("pr_list", "Open pull request operations", pullrequests.cmd_pr_list),
    ("pr_status", "Pull request status", pullrequests.cmd_pr_status),
    ("pr_settings", "PR automation settings", pullrequests.cmd_pr_settings),
    ("pr_history", "Pull request history", pullrequests.cmd_pr_history),
    ("achievements", "GitHub statistics", achievements.cmd_achievements),
    ("actions", "GitHub Actions", actions.cmd_actions),
    ("workflows", "Workflows", actions.cmd_workflows),
    ("runs", "Recent workflow runs", actions.cmd_runs),
    ("failed", "Failed workflow runs", actions.cmd_failed),
    ("rerun", "Re-run a workflow run", actions.cmd_rerun),
    ("cancel_run", "Cancel a workflow run", actions.cmd_cancel_run),
    ("profile", "GitHub profile", profile.cmd_profile),
    ("bio", "Change bio", profile.cmd_bio),
    ("name", "Change name", profile.cmd_name),
    ("location", "Change location", profile.cmd_location),
    ("website", "Change website", profile.cmd_website),
    ("socials", "Social links", profile.cmd_socials),
    ("social_add", "Add a social link", profile.cmd_social_add),
    ("social_remove", "Remove a social link", profile.cmd_social_remove),
    ("badges", "Profile badges", badges.cmd_badges),
    ("aboutme", "Profile README (about me)", aboutme.cmd_aboutme),
    ("dryrun", "Simulate a dangerous operation", safetycmds.cmd_dryrun),
    ("pending", "Operations waiting for confirmation", safetycmds.cmd_pending),
    ("operation", "Operation details", safetycmds.cmd_operation),
    ("lock", "Protect a repository", safetycmds.cmd_lock),
    ("unlock", "Remove protection", safetycmds.cmd_unlock),
    ("protected", "Protected repositories", safetycmds.cmd_protected),
    ("password", "Change the bot password", password.cmd_password),
    ("lockbot", "Lock the bot now", password.cmd_lockbot),
    ("settings", "Settings", common.cmd_settings),
    ("cancel", "Cancel pending (or /cancel id)", common.cmd_cancel),
]
COMMAND_HANDLERS = {name: fn for name, _, fn in COMMANDS}
HYPHEN_ALIASES = {name.replace("_", "-"): name for name in COMMAND_HANDLERS if "_" in name}
_ALIAS_RE = re.compile(r"^/([a-z]+(?:-[a-z]+)+)(?:@[A-Za-z0-9_]+)?(?:\s+(.*))?$", re.S)


async def hyphen_command(update: Update, context: Ctx) -> None:
    """Dispatch '/backup-all' style commands. Telegram would otherwise parse them as '/backup'."""
    message = update.effective_message
    match = _ALIAS_RE.match((message.text or "").strip()) if message else None
    if not match or match.group(1) not in HYPHEN_ALIASES:
        # Never let '/backup-foo' fall through to the '/backup' handler.
        await common.reply(update, context, "🤔 Unknown command. See /help.", edit=False)
        raise ApplicationHandlerStop
    context.args = (match.group(2) or "").split()
    try:
        await COMMAND_HANDLERS[HYPHEN_ALIASES[match.group(1)]](update, context)  # type: ignore[operator]
    except Exception as exc:  # noqa: BLE001 - route through the normal error handler
        context.error = exc
        await common.on_error(update, context)
    raise ApplicationHandlerStop


async def _expire_job(context: Ctx) -> None:
    services: Services = context.application.bot_data["services"]
    for op in await services.safety.expire_stale():
        try:
            await context.bot.send_message(services.settings.telegram_allowed_user_id,
                                           f"⌛ Operation #{op.id} ({h(op.kind)} on {code(op.repo)}) expired. Nothing was changed.")
        except Exception:  # noqa: BLE001
            pass


async def _autobackup_job(context: Ctx) -> None:
    services: Services = context.application.bot_data["services"]
    if not await services.bulk.autobackup_due():
        return
    try:
        result = await services.bulk.run_autobackup()
    except Exception as exc:  # noqa: BLE001
        log.error("Automatic backup failed: %s", exc)
        return
    if result.created or result.failed or result.pruned:
        if not services.lock.unlocked:
            text = "🤖 Automatic backup finished. Unlock the bot to see the details."
        else:
            text = f"🤖 Automatic backup: {h(result.summary())}"
            if result.failed:
                text += "\n" + "\n".join(f"❌ {h(x)}" for x in result.failed[:10])
        try:
            await context.bot.send_message(services.settings.telegram_allowed_user_id, text)
        except Exception:  # noqa: BLE001
            pass


async def _pr_monitor_job(context: Ctx) -> None:
    """Merge auto-merge pull requests once GitHub reports every required check green."""
    services: Services = context.application.bot_data["services"]

    async def notify(text: str) -> None:
        try:
            await context.bot.send_message(services.settings.telegram_allowed_user_id, text)
        except Exception:  # noqa: BLE001
            pass

    try:
        await services.pulls.auto_merge_round(notify)
    except Exception as exc:  # noqa: BLE001 - a background job must never crash the bot
        log.warning("Auto-merge round failed: %s", exc)


async def _post_init(app: Application) -> None:
    services: Services = app.bot_data["services"]
    await services.backups.cleanup_temp()
    interrupted = await services.safety.recover_on_startup()
    await app.bot.set_my_commands([BotCommand(name, desc) for name, desc, _ in COMMANDS])
    await services.load_active()
    mismatched: list[str] = []
    for username, resources in services.resources.items():
        try:
            login = (await resources.gh.get_user())["login"]
        except Exception:  # noqa: BLE001 - startup must not fail on a network hiccup
            continue
        if login.lower() != username.lower():
            log.warning("Account %s is configured, but its token belongs to %s", username, login)
            mismatched.append(f"{username} -> {login}")
    locked = await services.lock.is_configured()
    accounts_note = (f"👥 Accounts: {', '.join(services.usernames)} · active: {services.active}"
                     if len(services.usernames) > 1 else f"👤 Account: {services.active}")
    message = ("🤖 Bot started.\n" + accounts_note + "\n"
               + ("🔒 Locked: send your password to continue." if locked
                  else "🔐 No password set yet: send any message to set one."))
    if mismatched:
        message += "\n⚠️ Token/username mismatch in .env: " + h(", ".join(mismatched))
    if interrupted:
        message += "\n🛑 Interrupted operations (check these repositories):\n" + "\n".join(
            f"• #{op.id} {h(op.kind)} {code(op.repo)} backup {code(op.backup_id or 'none')}" for op in interrupted
        )
    try:
        await app.bot.send_message(services.settings.telegram_allowed_user_id, message)
    except Exception:  # noqa: BLE001 - user may not have started the bot yet
        log.info("Could not send startup message")


async def _post_shutdown(app: Application) -> None:
    await app.bot_data["services"].close()


def build_application(settings: Settings) -> Application:
    services = Services.build(settings)
    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .defaults(Defaults(parse_mode=ParseMode.HTML, link_preview_options=LinkPreviewOptions(is_disabled=True)))
        .arbitrary_callback_data(2048)
        .concurrent_updates(True)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.bot_data["services"] = services

    owner = filters.User(user_id=settings.telegram_allowed_user_id) & filters.ChatType.PRIVATE
    # Only new messages are processed: editing an old message never re-runs a command, input or confirmation.
    allowed = owner & filters.UpdateType.MESSAGE
    app.add_handler(TypeHandler(Update, common.auth_guard), group=-3)
    app.add_handler(TypeHandler(Update, password.lock_gate), group=-2)
    app.add_handler(MessageHandler(allowed & filters.Regex(r"^/[a-z]+(-[a-z]+)+"), hyphen_command), group=-1)
    for name, _, fn in COMMANDS:
        app.add_handler(CommandHandler(name, fn, filters=allowed))  # type: ignore[arg-type]
    app.add_handler(CallbackQueryHandler(common.on_invalid_callback, pattern=InvalidCallbackData))
    app.add_handler(CallbackQueryHandler(common.on_callback))
    app.add_handler(MessageHandler(allowed & filters.TEXT & ~filters.COMMAND, common.on_text))
    app.add_handler(MessageHandler(owner & filters.UpdateType.EDITED_MESSAGE, common.on_edited))
    app.add_error_handler(common.on_error)
    if app.job_queue:
        app.job_queue.run_repeating(_expire_job, interval=timedelta(seconds=60), first=timedelta(seconds=30))
        app.job_queue.run_repeating(_autobackup_job, interval=timedelta(minutes=15), first=timedelta(minutes=2),
                                    job_kwargs={"max_instances": 1, "coalesce": True})
        app.job_queue.run_repeating(_pr_monitor_job, interval=timedelta(seconds=90), first=timedelta(seconds=45),
                                    job_kwargs={"max_instances": 1, "coalesce": True})
    return app
