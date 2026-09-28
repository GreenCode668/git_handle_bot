"""/review and /review-issue: automated code review, Claude Code fixes, draft PR after approval."""

from __future__ import annotations

import secrets

from telegram import Update

from ghbot.bot.handlers.repos import repo_ref
from ghbot.bot.router import callback
from ghbot.bot.ui import Ctx, ProgressMessage, btn, code, h, join_limited, kb, reply, safe_error, svc, url_btn
from ghbot.services.review import SEVERITIES, ReviewError, ReviewResult, redact
from ghbot.validators import ValidationError

SEVERITY_EMOJI = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "⚪"}
MAX_FINDINGS_SHOWN = 25


def _chat_id(update: Update) -> int:
    return update.effective_chat.id if update.effective_chat else 0


def _parse_args(args: list[str]) -> tuple[str, int | None, bool]:
    force = any(a == "--force" for a in args)
    rest = [a for a in args if a != "--force"]
    if not rest:
        raise ValidationError("Usage: /review repo [commits] [--force]")
    depth = None
    if len(rest) > 1:
        if not rest[1].isdigit():
            raise ValidationError("The second argument must be a number of commits, e.g. /review my-repo 20")
        depth = int(rest[1])
    return rest[0], depth, force


def render_findings(result: ReviewResult) -> str:
    lines = [f"🔍 <b>Review of {h(result.repo)}</b>",
             f"Project: {h(result.project_type)} · commits: {len(result.commits)} · files: {len(result.files)}"]
    if result.read_only:
        lines.append("👁 Read-only (repository not owned): no linters, tests or fixes.")
    lines.append("")
    if not result.findings:
        lines.append("✅ No problems found. Nothing to fix.")
        return "\n".join(lines)
    grouped = result.by_severity()
    shown = 0
    for level in SEVERITIES:
        items = grouped[level]
        if not items:
            continue
        lines.append(f"<b>{SEVERITY_EMOJI[level]} {level.upper()} ({len(items)})</b>")
        for finding in items:
            if shown >= MAX_FINDINGS_SHOWN:
                lines.append(f"… and {len(result.findings) - shown} more")
                break
            lines.append(f"• <code>{h(finding.location)}</code> {h(finding.title)} — {h(finding.detail[:140])}")
            shown += 1
        lines.append("")
    for note in result.notes:
        lines.append(f"ℹ️ {h(note)}")
    return join_limited(lines)


async def _run_review(update: Update, context: Ctx, repo_text: str, depth: int | None, force: bool,
                      issue: int | None = None) -> None:
    s = svc(context)
    chat_id = _chat_id(update)
    repo = repo_ref(context, repo_text) if "/" not in repo_text else repo_ref(context, repo_text.split("/", 1)[1])
    label = f"/review {repo.name}" if issue is None else f"/review-issue {repo.name}#{issue}"
    try:
        s.review.start_run(chat_id, label)
    except ReviewError as exc:
        await reply(update, context, f"⏳ {h(exc)}", edit=False)
        return

    progress = await ProgressMessage.create(update, context, f"🔍 Reviewing {code(repo)}…")
    result: ReviewResult | None = None
    try:
        if issue is None:
            result = await s.review.review_commits(repo, depth or 20, force=force, progress=progress)
        else:
            result = await s.review.review_issue(repo, issue, force=force, progress=progress)
        await s.db.log_simple("review", repo.full_name, "done",
                              params={"issue": issue, "depth": depth},
                              result={"findings": len(result.findings)})
        text = render_findings(result)
        rows = []
        if result.fixable and not result.read_only:
            token = secrets.token_hex(4)
            context.user_data["review"] = {"token": token, "result": result, "repo": repo.full_name}  # type: ignore[index]
            rows.append([btn(f"🛠 Fix {len(result.fixable)} finding(s)", "rv_fix", token)])
            rows.append([btn("🗑 Discard clone", "rv_discard", token)])
            await progress.finish(context, update, text, kb(rows))
            return
        await progress.finish(context, update, text)
    except (ValidationError, ReviewError) as exc:
        await progress.finish(context, update, f"⚠️ {h(exc)}")
    except Exception as exc:  # noqa: BLE001
        await progress.finish(context, update, f"❌ Review failed: {safe_error(exc)}")
    finally:
        s.review.finish_run(chat_id)
        if result is not None and not (result.fixable and not result.read_only):
            await s.review.cleanup(result)  # nothing to fix: the clone goes away immediately


async def cmd_review(update: Update, context: Ctx) -> None:
    repo_text, depth, force = _parse_args(context.args or [])
    context.application.create_task(_run_review(update, context, repo_text, depth, force), update=update)


async def cmd_review_issue(update: Update, context: Ctx) -> None:
    args = [a for a in (context.args or []) if a != "--force"]
    force = "--force" in (context.args or [])
    if len(args) != 2 or not args[1].lstrip("#").isdigit():
        raise ValidationError("Usage: /review_issue repo 123")
    context.application.create_task(
        _run_review(update, context, args[0], None, force, issue=int(args[1].lstrip("#"))), update=update
    )


# ---------------------------------------------------------------------- fix
def _pending(context: Ctx, token: str) -> dict:
    pending = context.user_data.get("review")  # type: ignore[union-attr]
    if not pending or pending["token"] != token:
        raise ValidationError("That review expired. Run /review again.")
    return pending


@callback("rv_fix")
async def cb_fix(update: Update, context: Ctx, data: tuple) -> None:
    pending = _pending(context, data[1])
    context.application.create_task(_run_fix(update, context, pending), update=update)


async def _run_fix(update: Update, context: Ctx, pending: dict) -> None:
    s = svc(context)
    chat_id = _chat_id(update)
    result: ReviewResult = pending["result"]
    try:
        s.review.start_run(chat_id, "fix generation")
    except ReviewError as exc:
        await reply(update, context, f"⏳ {h(exc)}", edit=False)
        return
    progress = await ProgressMessage.create(update, context, "🛠 Generating a fix…")
    try:
        fix = await s.review.generate_fix(result, progress=progress)
    except (ReviewError, ValidationError) as exc:
        await progress.finish(context, update, f"⚠️ {h(exc)}")
        await s.review.cleanup(result)
        context.user_data.pop("review", None)  # type: ignore[union-attr]
        return
    except Exception as exc:  # noqa: BLE001
        await progress.finish(context, update, f"❌ Fix failed: {safe_error(exc)}")
        await s.review.cleanup(result)
        context.user_data.pop("review", None)  # type: ignore[union-attr]
        return
    finally:
        s.review.finish_run(chat_id)

    if not fix.verified:
        await s.db.log_simple("review_fix", result.repo, "failed", result={"reason": "verification failed"})
        await progress.finish(context, update, join_limited([
            "🛑 <b>Fix discarded: the linter or tests did not pass afterwards.</b>",
            "Nothing was pushed and no pull request was opened.", "",
            f"<pre>{h(fix.verification[-1200:])}</pre>",
        ]))
        await s.review.cleanup(result)
        context.user_data.pop("review", None)  # type: ignore[union-attr]
        return

    pending["fix"] = fix
    lines = [
        f"🛠 <b>Proposed fix for {h(result.repo)}</b>",
        f"Branch: <code>{h(fix.branch)}</code> · files changed: {len(fix.changed_files)}",
        "✅ Linter and tests pass after the change.", "",
        f"<pre>{h(fix.diff_stat[:800])}</pre>",
        "<b>First lines of the diff</b>",
        f"<pre>{h(fix.diff_head[:2000])}</pre>", "",
    ]
    if fix.claude_summary:
        lines += [f"<i>{h(fix.claude_summary[:400])}</i>", ""]
    lines.append("Nothing has been pushed. Approve to push the branch and open a <b>draft</b> pull request.")
    await progress.finish(context, update, join_limited(lines),
                          kb([[btn("✅ Approve: push & open draft PR", "rv_approve", pending["token"])],
                              [btn("✖️ Reject and delete clone", "rv_discard", pending["token"])]]))


@callback("rv_approve")
async def cb_approve(update: Update, context: Ctx, data: tuple) -> None:
    pending = _pending(context, data[1])
    if "fix" not in pending:
        raise ValidationError("No prepared fix. Run /review again.")
    context.application.create_task(_run_push(update, context, pending), update=update)


async def _run_push(update: Update, context: Ctx, pending: dict) -> None:
    s = svc(context)
    result, fix = pending["result"], pending["fix"]
    repo = repo_ref(context, pending["repo"].split("/", 1)[1])
    progress = await ProgressMessage.create(update, context, "🚀 Pushing the fix branch…")
    try:
        created = await s.review.commit_push_and_pr(repo, result, fix, progress=progress)
    except Exception as exc:  # noqa: BLE001
        await progress.finish(context, update, f"❌ Push failed: {safe_error(exc)}")
        return
    finally:
        await s.review.cleanup(result)
        context.user_data.pop("review", None)  # type: ignore[union-attr]
    await progress.finish(context, update, join_limited([
        f"✅ <b>Draft pull request #{created['number']} opened</b>",
        f"{h(created['url'])}", "",
        "It is a draft and will not be merged by the bot. Review it, mark it ready, then merge it yourself "
        "or with /pr_status.",
    ]), kb([[url_btn("🔗 Open pull request", created["url"])]]))


@callback("rv_discard")
async def cb_discard(update: Update, context: Ctx, data: tuple) -> None:
    pending = context.user_data.pop("review", None)  # type: ignore[union-attr]
    if pending and pending.get("token") == data[1]:
        await svc(context).review.cleanup(pending["result"])
    await reply(update, context, "🗑 Discarded. The temporary clone was deleted and nothing was pushed.")


async def cmd_review_status(update: Update, context: Ctx) -> None:
    s = svc(context)
    running = s.review.busy_with(_chat_id(update))
    available = "installed" if s.review.claude_available() else "NOT installed"
    await reply(update, context, join_limited([
        "🔍 <b>Review status</b>",
        f"Running now: {h(running) if running else 'nothing'}",
        f"Claude Code CLI: <b>{available}</b> ({code(s.settings.claude_code_path)})",
        f"Timeout: {s.settings.review_timeout_ms // 1000}s · max files per fix: {s.settings.max_files_per_fix}",
        f"Clone directory: {code(s.settings.review_dir)}",
        "" if s.review.claude_available() else
        "\nInstall it with <code>curl -fsSL https://claude.ai/install.sh | bash</code>, then set "
        "<code>CLAUDE_CODE_PATH</code> in .env and restart.",
    ]), edit=False)


__all__ = ["cmd_review", "cmd_review_issue", "cmd_review_status", "redact", "render_findings"]
