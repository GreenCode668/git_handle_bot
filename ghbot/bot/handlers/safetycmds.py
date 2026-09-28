"""Safety commands: dry runs, pending operations, operation details, manual protection."""

from __future__ import annotations

from typing import Any

from telegram import Update

from ghbot.bot.handlers.operations import STAGE_EMOJI, render_operation, start_operation
from ghbot.bot.handlers.repos import readable, repo_ref
from ghbot.bot.router import callback
from ghbot.bot.ui import Ctx, ProgressMessage, ago, btn, code, h, join_limited, kb, reply, safe_error, svc
from ghbot.services.operations import DEFAULT_SQUASH_STYLE
from ghbot.services.safety import WAITING, SafetyError
from ghbot.validators import (
    RepoRef,
    ValidationError,
    parse_date,
    parse_repo_action,
    validate_backup_id,
    validate_git_url,
    validate_repo_name,
)

DRYRUN_HELP = """🧪 <b>Dry run</b>: simulate without modifying anything
<code>/dryrun delete repo</code>
<code>/dryrun rewrite repo YYYY-MM-DD [snapshot|drop]</code>
<code>/dryrun restore BK-YYYYMMDD-NNN [repo]</code>
<code>/dryrun rename repo new-name</code>
<code>/dryrun archive|unarchive|private repo</code>
<code>/dryrun create name</code>
<code>/dryrun import https://… name</code>
<code>/dryrun repo @remove</code> (quick-action syntax also works)"""

SIMPLE_VERBS = {"delete": "delete_repo", "remove": "delete_repo", "archive": "archive_repo",
                "unarchive": "unarchive_repo", "private": "make_private", "public": "make_public"}


async def parse_dryrun(context: Ctx, args: list[str]) -> tuple[str, RepoRef, dict[str, Any]]:
    if not args:
        raise ValidationError("Missing operation.")
    action = parse_repo_action(" ".join(args))
    if action is not None:
        if action.action in ("info", "commits", "branches", "actions", "backup", "stats"):
            raise ValidationError(f"@{action.action} is read-only; nothing to simulate.")
        args = ["rename", action.repo, action.arg or ""] if action.action == "rename" else [action.action, action.repo]
    verb, rest = args[0].lower(), args[1:]
    if verb in SIMPLE_VERBS and len(rest) == 1:
        return SIMPLE_VERBS[verb], repo_ref(context, rest[0]), {}
    if verb == "rename" and len(rest) == 2:
        return "rename_repo", repo_ref(context, rest[0]), {"new_name": validate_repo_name(rest[1])}
    if verb == "rewrite" and len(rest) in (2, 3):
        policy = rest[2] if len(rest) == 3 else "snapshot"
        if policy not in ("snapshot", "drop"):
            raise ValidationError("Tag policy must be snapshot or drop.")
        return "rewrite_history", repo_ref(context, rest[0]), {"cutoff": parse_date(rest[1]).isoformat(), "tag_policy": policy, "squash_style": DEFAULT_SQUASH_STYLE}
    if verb == "restore" and len(rest) in (1, 2):
        backup_id = validate_backup_id(rest[0])
        record = await svc(context).db.get_backup(backup_id)
        if record is None:
            raise ValidationError("Backup not found.")
        target = repo_ref(context, rest[1]) if len(rest) == 2 else repo_ref(context, record.repo)
        return "restore_backup", target, {"backup_id": backup_id}
    if verb == "create" and len(rest) == 1:
        return "create_repo", repo_ref(context, rest[0]), {"description": "", "private": False}
    if verb == "import" and len(rest) == 2:
        return "import_repo", repo_ref(context, rest[1]), {"url": validate_git_url(rest[0]), "private": False}
    raise ValidationError("Unrecognized dry run.")


async def cmd_dryrun(update: Update, context: Ctx) -> None:
    try:
        kind, repo, params = await parse_dryrun(context, context.args or [])
    except ValidationError as exc:
        await reply(update, context, f"⚠️ {h(exc)}\n\n{DRYRUN_HELP}", edit=False)
        return
    progress = await ProgressMessage.create(update, context, "🧪 Simulating… nothing will be changed, locked or backed up.")
    try:
        op = await svc(context).safety.dry_run(kind, repo, params, progress)
    except SafetyError as exc:
        await progress.finish(context, update, h(exc))
        return
    except Exception as exc:  # noqa: BLE001
        await progress.finish(context, update, f"❌ Dry run failed: {safe_error(exc)}")
        return
    text, markup = render_operation(context, op)
    await progress.finish(context, update, text, markup)


async def show_pending(update: Update, context: Ctx) -> None:
    ops = await svc(context).db.operations_in_stages(list(WAITING))
    lines = ["⏳ <b>Operations waiting for you</b>", ""]
    rows = []
    for op in ops:
        expires = op.expires_at.strftime("%H:%M UTC") if op.expires_at else "—"
        step = "typed confirmation" if op.stage == "awaiting_confirmation" else "approval"
        lines.append(f"{STAGE_EMOJI.get(op.stage, '•')} #{op.id} {h(op.kind)} {code(op.repo or '')} · needs {step} · expires {expires}")
        rows.append([btn(f"#{op.id} open", "op", op.id), btn("✖️ Cancel", "op_cancel", op.id)])
    if not ops:
        lines.append("Nothing is waiting for confirmation.")
    await reply(update, context, "\n".join(lines), kb(rows) if rows else None)


async def cmd_pending(update: Update, context: Ctx) -> None:
    await show_pending(update, context)


@callback("pending")
async def cb_pending(update: Update, context: Ctx, data: tuple) -> None:
    await show_pending(update, context)


def _op_id(text: str) -> int:
    text = text.strip().lstrip("#")
    if not text.isdigit():
        raise ValidationError("Operation id must be a number, e.g. /operation 42")
    return int(text)


async def cmd_operation(update: Update, context: Ctx) -> None:
    if not context.args:
        raise ValidationError("Usage: /operation id")
    s = svc(context)
    op = await s.db.get_operation(_op_id(context.args[0]))
    if op is None:
        raise ValidationError("Operation not found.")
    text, markup = render_operation(context, op)
    details = [
        "", "<b>Record</b>",
        f"Kind: {code(op.kind)} · Stage: {code(op.stage)}",
        f"Created: {ago(op.created_at)} · Updated: {ago(op.updated_at)} · Expires: {ago(op.expires_at)}",
        f"Backup: {code(op.backup_id or '—')} · Confirmation: {code(op.confirm_phrase or 'button/none')}",
        f"Parameters: {code(op.params)}",
    ]
    if op.error:
        details.append(f"Error: {h(op.error)}")
    await reply(update, context, join_limited([text, *details]), markup, edit=False)


async def cancel_operation_by_id(update: Update, context: Ctx, raw: str) -> None:
    s = svc(context)
    op_id = _op_id(raw)
    op = await s.db.get_operation(op_id)
    if op is None:
        raise ValidationError("Operation not found.")
    if await s.safety.cancel(op_id):
        await reply(update, context, f"✖️ Operation #{op_id} ({h(op.kind)}) cancelled. Nothing was changed.", edit=False)
    else:
        await reply(update, context, f"ℹ️ Operation #{op_id} is {h(op.stage)} and cannot be cancelled.", edit=False)


# ------------------------------------------------------------ manual protection
async def protect(update: Update, context: Ctx, text: str, reason: str | None) -> None:
    repo, meta = await readable(context, text)
    added = await svc(context).db.protect_repo(meta["full_name"], reason)
    await svc(context).db.log_simple("protect_repo", meta["full_name"], "done", params={"reason": reason})
    await reply(update, context, f"🛡 {code(meta['full_name'])} {'is now' if added else 'was already'} manually protected. "
                                 "All write and destructive operations on it are blocked until /unlock.", edit=False)


async def cmd_lock(update: Update, context: Ctx) -> None:
    if not context.args:
        raise ValidationError("Usage: /lock repo [reason]")
    await protect(update, context, context.args[0], " ".join(context.args[1:])[:200] or None)


@callback("lock_do")
async def cb_lock(update: Update, context: Ctx, data: tuple) -> None:
    await protect(update, context, data[1], None)


async def cmd_unlock(update: Update, context: Ctx) -> None:
    if not context.args:
        raise ValidationError("Usage: /unlock repo")
    await start_operation(update, context, "unlock_repo", repo_ref(context, context.args[0]), {})


@callback("unlock_ask")
async def cb_unlock(update: Update, context: Ctx, data: tuple) -> None:
    await start_operation(update, context, "unlock_repo", repo_ref(context, data[1]), {})


async def show_protected(update: Update, context: Ctx) -> None:
    protected = await svc(context).db.list_protected()
    lines = ["🛡 <b>Manually protected repositories</b>", ""]
    rows = []
    for p in protected:
        lines.append(f"• {code(p['full_name'])} since {ago(p['created_at'])}" + (f" — {h(p['reason'])}" if p.get("reason") else ""))
        rows.append([btn(f"🔓 Unlock {p['full_name'].split('/', 1)[1]}", "unlock_ask", p["full_name"])])
    if not protected:
        lines.append("None. Use /lock repo to block write/destructive operations on a repository.")
    await reply(update, context, "\n".join(lines), kb(rows) if rows else None)


async def cmd_protected(update: Update, context: Ctx) -> None:
    await show_protected(update, context)


@callback("protected")
async def cb_protected(update: Update, context: Ctx, data: tuple) -> None:
    await show_protected(update, context)
