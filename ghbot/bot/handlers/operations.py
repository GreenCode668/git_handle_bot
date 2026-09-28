"""Rendering and driving safety-engine operations from Telegram."""

from __future__ import annotations

from typing import Any

from telegram import InlineKeyboardMarkup, Update

from ghbot.bot.router import callback
from ghbot.bot.ui import Ctx, ProgressMessage, btn, code, h, join_limited, kb, reply, safe_error, svc
from ghbot.db import Operation
from ghbot.services.safety import SafetyError, Stage
from ghbot.validators import RepoRef

STAGE_EMOJI = {
    Stage.ANALYZED: "🔍", Stage.BACKING_UP: "💾", Stage.AWAITING_CONFIRMATION: "⚠️", Stage.EXECUTING: "⚙️",
    Stage.DONE: "✅", Stage.FAILED: "❌", Stage.CANCELLED: "✖️", Stage.EXPIRED: "⌛", Stage.INTERRUPTED: "🛑",
}


def render_generic(op: Operation) -> tuple[str, InlineKeyboardMarkup | None]:
    """Operations that are not safety-engine specs: dry runs and simple logged actions."""
    impact: dict[str, Any] = op.impact or {}
    result = op.result or {}
    if op.kind == "dryrun":
        would = result.get("would_proceed")
        lines = [f"🧪 <b>Dry run: {h(result.get('simulated', op.params.get('operation')))}</b> on {code(op.repo)}  <i>#{op.id}</i>",
                 "<i>Simulation only. Nothing was changed, locked or backed up.</i>", ""]
        lines += [f"• {h(line)}" for line in impact.get("lines", [])]
        if impact.get("warnings"):
            lines += ["", "<b>Warnings</b>"] + [f"⚠️ {h(w)}" for w in impact["warnings"]]
        lines += ["", "<b>Checks</b>"]
        lines += [f"{'✅' if ok else '❌'} {h(name)}{(' — ' + h(detail)) if detail else ''}" for name, ok, detail in result.get("checks", [])]
        lines += ["", "✅ <b>The real operation could proceed</b> (it would still require your confirmation)." if would
                  else "🛑 <b>The real operation would be blocked.</b>"]
        return join_limited(lines), None
    lines = [f"{STAGE_EMOJI.get(op.stage, '•')} <b>{h(op.kind)}</b> {code(op.repo or '')}  <i>#{op.id}</i>", f"Stage: {h(op.stage)}"]
    lines += [f"• {h(k)}: {h(v)}" for k, v in op.params.items()]
    lines += [f"• {h(k)}: {h(v)}" for k, v in result.items()]
    if op.error:
        lines.append(f"Error: {h(op.error)}")
    return join_limited(lines), None


def render_operation(context: Ctx, op: Operation) -> tuple[str, InlineKeyboardMarkup | None]:
    engine = svc(context).safety
    if op.kind not in engine.specs:
        return render_generic(op)
    spec = engine.spec(op.kind)
    impact: dict[str, Any] = op.impact or {}
    lines = [f"{STAGE_EMOJI.get(op.stage, '•')} <b>{h(impact.get('title') or spec.label)}</b>  <i>#{op.id}</i>", ""]
    lines += [f"• {h(line)}" for line in impact.get("lines", [])]
    if impact.get("warnings"):
        lines += ["", "<b>Warnings</b>"] + [f"⚠️ {h(w)}" for w in impact["warnings"]]
    if impact.get("blockers"):
        lines += ["", "<b>Blocked</b>"] + [f"🛑 {h(b)}" for b in impact["blockers"]]

    rows: list[list] = []
    needs_backup = spec.requires_backup and impact.get("target_exists", True)
    if op.stage == Stage.ANALYZED:
        lines.append("")
        if op.kind == "rewrite_history":
            policy = op.params.get("tag_policy", "snapshot")
            removal = "squashed into one summary commit" if op.params.get("squash_style", "summary") == "summary" else "removed"
            lines.append(f"Strategy: <b>{h(impact.get('data', {}).get('strategy', ''))}</b>: current files on every "
                         f"branch are kept byte-for-byte; only pre-cutoff commits are {removal}.")
            other = "drop" if policy == "snapshot" else "snapshot"
            rows.append([btn("🏷 Delete old tags" if other == "drop" else "🏷 Keep old tags as snapshots",
                             "op_policy", op.id, other)])
        if spec.confirm_mode == "button":
            lines.append("Nothing has been changed yet.")
            rows.append([btn("✅ Confirm", "op_confirm", op.id), btn("✖️ Cancel", "op_cancel", op.id)])
        else:
            step = "create and verify a full mirror backup" if needs_backup else "continue to final confirmation"
            lines.append(f"Nothing has been changed yet. Next step: {step}.")
            rows.append([btn("💾 Continue: backup" if needs_backup else "➡️ Continue", "op_approve", op.id),
                         btn("✖️ Cancel", "op_cancel", op.id)])
    elif op.stage == Stage.AWAITING_CONFIRMATION:
        lines.append("")
        if op.backup_id:
            lines.append(f"✅ Backup {code(op.backup_id)} created and verified.")
        elif needs_backup is False and spec.requires_backup:
            lines.append("ℹ️ Target does not exist, so there is nothing to back up.")
        expires = op.expires_at.strftime("%H:%M UTC") if op.expires_at else "soon"
        lines.append(f"\n<b>Final confirmation.</b> Send exactly:\n{code(op.confirm_phrase)}\n(expires {expires})")
        rows.append([btn("✖️ Cancel", "op_cancel", op.id)])
    elif op.stage == Stage.DONE:
        lines += ["", "✅ <b>Completed and verified.</b>"]
        lines += _result_lines(op)
    elif op.stage in (Stage.FAILED, Stage.CANCELLED, Stage.EXPIRED, Stage.INTERRUPTED):
        lines += ["", f"<b>{h(op.stage.upper())}</b>: {h(op.error or '')}"]
        if op.backup_id:
            lines.append(f"Backup: {code(op.backup_id)}")
            rows.append([btn("🔎 Backup details", "bk", op.backup_id)])
        lines += _result_lines(op)
    else:
        lines += ["", f"Stage: {h(op.stage)}"]
    return join_limited(lines), kb(rows) if rows else None


def _result_lines(op: Operation) -> list[str]:
    result = op.result or {}
    lines = []
    for key, value in result.items():
        if key == "expected_refs":
            lines.append(f"• refs on GitHub: {len(value)}")
        else:
            lines.append(f"• {h(key.replace('_', ' '))}: {h(value)}")
    if op.backup_id and op.kind in ("delete_repo", "rewrite_history", "restore_backup"):
        lines.append(f"↩️ Undo with /undo or /restore {h(op.backup_id)}")
    return lines


async def start_operation(update: Update, context: Ctx, kind: str, repo: RepoRef, params: dict[str, Any]) -> None:
    progress = await ProgressMessage.create(update, context, "🔍 Analyzing… nothing will be changed.")
    try:
        op = await svc(context).safety.start(kind, repo, params, progress)
    except SafetyError as exc:
        await progress.finish(context, update, h(exc))
        return
    except Exception as exc:  # noqa: BLE001
        await progress.finish(context, update, f"❌ Analysis failed: {safe_error(exc)}")
        return
    text, markup = render_operation(context, op)
    await progress.finish(context, update, text, markup)


@callback("op")
async def cb_show(update: Update, context: Ctx, data: tuple) -> None:
    op = await svc(context).db.get_operation(int(data[1]))
    if op is None:
        await reply(update, context, "Operation not found.")
        return
    text, markup = render_operation(context, op)
    await reply(update, context, text, markup)


@callback("op_approve")
async def cb_approve(update: Update, context: Ctx, data: tuple) -> None:
    progress = ProgressMessage(update.callback_query.message if update.callback_query else None)  # type: ignore[arg-type]
    await progress("⏳ Starting…")
    try:
        op = await svc(context).safety.approve(int(data[1]), progress)
    except SafetyError as exc:
        await progress.finish(context, update, h(exc))
        return
    text, markup = render_operation(context, op)
    await progress.finish(context, update, text, markup)


@callback("op_confirm")
async def cb_confirm(update: Update, context: Ctx, data: tuple) -> None:
    progress = ProgressMessage(update.callback_query.message if update.callback_query else None)  # type: ignore[arg-type]
    try:
        op = await svc(context).safety.confirm_button(int(data[1]), progress)
    except SafetyError as exc:
        await progress.finish(context, update, h(exc))
        return
    text, markup = render_operation(context, op)
    await progress.finish(context, update, text, markup)


@callback("op_cancel")
async def cb_cancel(update: Update, context: Ctx, data: tuple) -> None:
    engine = svc(context).safety
    cancelled = await engine.cancel(int(data[1]))
    op = await svc(context).db.get_operation(int(data[1]))
    if op is None:
        return
    text, markup = render_operation(context, op)
    if not cancelled:
        text = "ℹ️ Operation can no longer be cancelled.\n\n" + text
    await reply(update, context, text, markup)


@callback("op_policy")
async def cb_policy(update: Update, context: Ctx, data: tuple) -> None:
    policy = data[2] if data[2] in ("snapshot", "drop") else "snapshot"
    op = await svc(context).db.get_operation(int(data[1]))
    if op is None or op.stage != Stage.ANALYZED:
        await reply(update, context, "Operation is no longer pending.")
        return
    await svc(context).safety.cancel(op.id)
    await start_operation(update, context, op.kind, RepoRef(*op.repo.split("/", 1)), {**op.params, "tag_policy": policy})  # type: ignore[union-attr]


async def handle_confirmation_text(update: Update, context: Ctx, text: str) -> bool:
    """Returns True if the text was a pending confirmation phrase."""
    engine = svc(context).safety
    op = await svc(context).db.find_awaiting_confirmation(text.strip(), Stage.AWAITING_CONFIRMATION)
    if op is None:
        return False
    progress = await ProgressMessage.create(update, context, f"⚙️ Confirmation accepted for #{op.id}…")
    try:
        result = await engine.confirm_phrase(text, progress)
    except SafetyError as exc:
        await progress.finish(context, update, h(exc))
        return True
    if result is None:
        await progress.finish(context, update, "Confirmation phrase did not match any pending operation.")
        return True
    rendered, markup = render_operation(context, result)
    await progress.finish(context, update, rendered, markup)
    return True
