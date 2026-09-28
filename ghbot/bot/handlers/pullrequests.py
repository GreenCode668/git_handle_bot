"""🏆 PR Automation: prepare a real change, open a pull request, watch checks, merge when allowed."""

from __future__ import annotations

from telegram import Update

from ghbot.bot.handlers.operations import start_operation
from ghbot.bot.handlers.repos import readable, repo_ref, show_repo_list
from ghbot.bot.router import await_input, callback, clear_input, text_input
from ghbot.bot.ui import Ctx, ProgressMessage, ago, btn, code, h, join_limited, kb, pager, reply, safe_error, svc, url_btn
from ghbot.github.client import GitHubError
from ghbot.services.pullrequests import MERGE_METHODS, PR_DEFAULTS
from ghbot.validators import ValidationError

STATUS_EMOJI = {"opening": "⏳", "open": "🔀", "merging": "⏳", "merged": "✅", "closed": "🚪", "failed": "❌"}

CHANGE_TYPES = {
    "readme": ("📄 Documentation / README", "Append text to a documentation file."),
    "replace": ("🔢 Dependency or version bump", "Replace one exact piece of text, e.g. a version string."),
    "whitespace": ("🧹 Formatting cleanup", "Strip trailing whitespace and fix the final newline."),
    "file": ("✍️ Provide file content", "Send the full new content of a file."),
}


async def show_menu(update: Update, context: Ctx) -> None:
    s = svc(context)
    settings = await s.pulls.settings()
    active, total = await s.db.list_pull_requests(account=s.active, statuses=["opening", "open", "merging"], limit=5)
    lines = [f"🏆 <b>PR Automation</b> · account {code(s.active)}", "",
             f"Open pull request operations: {total}",
             f"Auto-merge: <b>{'ON' if settings['pr_auto_merge'] else 'OFF'}</b> · "
             f"method: {h(settings['pr_merge_method'])} · "
             f"checks required: {'yes' if settings['pr_require_checks'] else 'no'}", ""]
    for record in active:
        lines.append(f"{STATUS_EMOJI.get(record.status, '•')} {h(record.repo.split('/', 1)[1])} "
                     f"#{record.number or '—'} {h(record.title[:50])}")
    lines += ["", "Pull requests are for real maintenance changes. The bot never merges past branch protection, "
              "required reviews or failing checks."]
    rows = [
        [btn("➕ New pull request", "pr_new")],
        [btn("📋 Open pull requests", "pr_list", 1), btn("🧾 History", "pr_history", 1)],
        [btn("⚙️ Settings", "pr_settings"), btn("🏅 Achievements", "achievements")],
        [btn("📖 Guide", "guide", "prs")],
    ]
    await reply(update, context, join_limited(lines), kb(rows))


async def cmd_pr(update: Update, context: Ctx) -> None:
    await show_menu(update, context)


@callback("pr_menu")
async def cb_menu(update: Update, context: Ctx, data: tuple) -> None:
    await show_menu(update, context)


# --------------------------------------------------------------- create flow
async def cmd_pr_create(update: Update, context: Ctx) -> None:
    if context.args:
        await start_change(update, context, context.args[0])
        return
    await show_repo_list(update, context, "pr", 1)


@callback("pr_new")
async def cb_new(update: Update, context: Ctx, data: tuple) -> None:
    await show_repo_list(update, context, "pr", 1)


async def start_change(update: Update, context: Ctx, repo_text: str) -> None:
    repo, _ = await readable(context, repo_text)
    s = svc(context)
    if not await s.pulls.repo_allowed(repo):
        raise ValidationError(f"{repo.full_name} is not in the allowed repositories list (/pr_settings).")
    context.user_data["pr"] = {"repo": repo.full_name}  # type: ignore[index]
    rows = [[btn(label, "pr_kind", kind)] for kind, (label, _) in CHANGE_TYPES.items()]
    rows.append([btn("✖️ Cancel", "pr_cancel")])
    lines = [f"➕ <b>New pull request</b> · {code(repo)}", "", "What change do you want to make?", ""]
    lines += [f"{label} — {h(hint)}" for label, hint in ((v[0], v[1]) for v in CHANGE_TYPES.values())]
    await reply(update, context, "\n".join(lines), kb(rows))


@callback("pr_kind")
async def cb_kind(update: Update, context: Ctx, data: tuple) -> None:
    pending = context.user_data.get("pr")  # type: ignore[union-attr]
    if not pending:
        await reply(update, context, "Start again with /pr_create.")
        return
    kind = data[1]
    if kind not in CHANGE_TYPES:
        return
    pending["change_kind"] = kind
    prompts = {
        "readme": ("📄 Send the text to add, optionally as <code>path | text</code> "
                   "(default path README.md)."),
        "replace": ("🔢 Send <code>path | old text | new text</code>, e.g.\n"
                    "<code>package.json | \"version\": \"1.0.0\" | \"version\": \"1.0.1\"</code>"),
        "whitespace": "🧹 Send the file path to clean, e.g. <code>src/main.py</code>.",
        "file": "✍️ Send <code>path | full new content</code> (the file is created if missing).",
    }
    await_input(context, "pr_change")
    await reply(update, context, prompts[kind] + "\n\n/cancel to abort.")


@text_input("pr_change")
async def input_change(update: Update, context: Ctx, state: dict, text: str) -> None:
    pending = context.user_data.get("pr")  # type: ignore[union-attr]
    if not pending:
        await reply(update, context, "Start again with /pr_create.", edit=False)
        return
    kind = pending["change_kind"]
    parts = [p.strip() for p in text.split("|")]
    params = {"change_kind": kind}
    if kind == "readme":
        params["path"], params["text"] = (parts[0], parts[1]) if len(parts) >= 2 else ("README.md", text.strip())
    elif kind == "replace":
        if len(parts) != 3:
            raise ValidationError("Send <code>path | old text | new text</code>.")
        params["path"], params["old_str"], params["new_str"] = parts
    elif kind == "whitespace":
        params["path"] = parts[0]
    else:
        if len(parts) < 2:
            raise ValidationError("Send <code>path | full new content</code>.")
        params["path"], params["content"] = parts[0], text.split("|", 1)[1].strip()
    clear_input(context)
    pending.update(params)
    await ask_message(update, context, pending)


async def ask_message(update: Update, context: Ctx, pending: dict) -> None:
    await_input(context, "pr_message")
    await reply(update, context, "💬 Send the commit message and pull request title (one line), "
                                 "or tap to use the default.",
                kb([[btn("Use default message", "pr_default_msg")], [btn("✖️ Cancel", "pr_cancel")]]), edit=False)


@text_input("pr_message")
async def input_message(update: Update, context: Ctx, state: dict, text: str) -> None:
    pending = context.user_data.get("pr")  # type: ignore[union-attr]
    if not pending:
        return
    pending["message"] = text.strip()
    pending["title"] = text.strip()
    clear_input(context)
    await launch(update, context, pending)


@callback("pr_default_msg")
async def cb_default_msg(update: Update, context: Ctx, data: tuple) -> None:
    pending = context.user_data.get("pr")  # type: ignore[union-attr]
    if not pending:
        await reply(update, context, "Start again with /pr_create.")
        return
    clear_input(context)
    await launch(update, context, pending)


async def launch(update: Update, context: Ctx, pending: dict) -> None:
    repo = repo_ref(context, pending.pop("repo"))
    context.user_data.pop("pr", None)  # type: ignore[union-attr]
    await start_operation(update, context, "open_pr", repo, dict(pending))


@callback("pr_cancel")
async def cb_cancel(update: Update, context: Ctx, data: tuple) -> None:
    context.user_data.pop("pr", None)  # type: ignore[union-attr]
    clear_input(context)
    await reply(update, context, "✖️ Cancelled. Nothing was changed.")


# ------------------------------------------------------------------- listing
async def show_list(update: Update, context: Ctx, page: int, history: bool) -> None:
    s = svc(context)
    per_page = 8
    statuses = None if history else ["opening", "open", "merging"]
    records, total = await s.db.list_pull_requests(account=s.active, statuses=statuses,
                                                   limit=per_page, offset=(page - 1) * per_page)
    title = "🧾 <b>Pull request history</b>" if history else "📋 <b>Open pull request operations</b>"
    lines = [f"{title} ({total})", ""]
    rows = []
    for record in records:
        lines.append(f"{STATUS_EMOJI.get(record.status, '•')} <b>{h(record.repo.split('/', 1)[1])}</b> "
                     f"#{record.number or '—'} {h(record.title[:60])}\n"
                     f"    {h(record.change_type)} · {h(record.status)} · {ago(record.created_at)}"
                     + (f" · ❌ {h(record.error[:60])}" if record.error else ""))
        rows.append([btn(f"{STATUS_EMOJI.get(record.status, '•')} #{record.number or record.id} "
                         f"{record.repo.split('/', 1)[1]}"[:60], "pr_show", record.id)])
    if not records:
        lines.append("Nothing yet." if history else "No open pull request operations.")
    last = max(1, -(-total // per_page))
    rows.append(pager("pr_history" if history else "pr_list", page, page < last, last_page=last))
    rows.append([btn("⬅️ PR menu", "pr_menu")])
    await reply(update, context, join_limited(lines), kb(rows))


@callback("pr_list")
async def cb_list(update: Update, context: Ctx, data: tuple) -> None:
    await show_list(update, context, int(data[1]), history=False)


@callback("pr_history")
async def cb_history(update: Update, context: Ctx, data: tuple) -> None:
    await show_list(update, context, int(data[1]), history=True)


async def cmd_pr_list(update: Update, context: Ctx) -> None:
    await show_list(update, context, 1, history=False)


async def cmd_pr_history(update: Update, context: Ctx) -> None:
    await show_list(update, context, 1, history=True)


# -------------------------------------------------------------------- status
async def show_status(update: Update, context: Ctx, record_id: int) -> None:
    s = svc(context)
    record = await s.db.get_pull_request(record_id)
    if record is None:
        raise ValidationError("Pull request not found.")
    repo = repo_ref(context, record.repo)
    lines = [f"{STATUS_EMOJI.get(record.status, '•')} <b>{h(record.title)}</b>",
             f"{code(record.repo)} · #{record.number or '—'} · {h(record.change_type)}",
             f"Branch {code(record.branch)} → {code(record.base)}",
             f"Recorded status: {h(record.status)} · auto-merge {'ON' if record.auto_merge else 'OFF'}"]
    rows = []
    if record.number and record.status in ("open", "merging"):
        progress = await ProgressMessage.create(update, context, "🔍 Reading checks and reviews…")
        try:
            status = await s.pulls.status(repo, record.number)
        except GitHubError as exc:
            await progress.finish(context, update, f"❌ {safe_error(exc)}")
            return
        settings = await s.pulls.settings()
        problems = status.blockers(bool(settings["pr_require_checks"]))
        lines += [
            "",
            f"State: {h(status.state)}{' (draft)' if status.draft else ''} · "
            f"mergeable: {status.mergeable} ({h(status.mergeable_state)})",
            f"✅ {len(status.checks.passed)} passed · ❌ {len(status.checks.failed)} failed · "
            f"⏳ {len(status.checks.pending)} pending",
        ]
        if status.checks.failed:
            lines.append("Failed: " + h(", ".join(status.checks.failed[:6])))
        if status.checks.pending:
            lines.append("Pending: " + h(", ".join(status.checks.pending[:6])))
        if status.checks.unreadable:
            lines.append("⚠️ Check results unreadable (token needs Checks: read / Commit statuses: read).")
        lines.append(f"Reviews: {status.approvals} approval(s)"
                     + (f" of {status.required_reviews} required" if status.required_reviews else "")
                     + (f" · {status.changes_requested} requested changes" if status.changes_requested else ""))
        lines += ["", "🛑 Cannot merge yet:" if problems else "✅ Ready to merge."]
        lines += [f"• {h(p)}" for p in problems]
        if not problems:
            rows.append([btn("🔀 Merge now", "pr_merge", record.id)])
        rows.append([url_btn("🔗 Open on GitHub", status.html_url)])
        await progress.finish(context, update, join_limited(lines),
                              kb([*rows, [btn("🔄 Refresh", "pr_show", record.id), btn("⬅️ PR menu", "pr_menu")]]))
        return
    if record.html_url:
        rows.append([url_btn("🔗 Open on GitHub", record.html_url)])
    if record.error:
        lines.append(f"\nError: {h(record.error)}")
    rows.append([btn("⬅️ PR menu", "pr_menu")])
    await reply(update, context, join_limited(lines), kb(rows))


@callback("pr_show")
async def cb_show(update: Update, context: Ctx, data: tuple) -> None:
    await show_status(update, context, int(data[1]))


async def cmd_pr_status(update: Update, context: Ctx) -> None:
    s = svc(context)
    if context.args and context.args[0].isdigit():
        await show_status(update, context, int(context.args[0]))
        return
    records, _ = await s.db.list_pull_requests(account=s.active, statuses=["opening", "open", "merging"], limit=1)
    if not records:
        await reply(update, context, "No open pull request operations. Start one with /pr_create.", edit=False)
        return
    await show_status(update, context, records[0].id)


@callback("pr_merge")
async def cb_merge(update: Update, context: Ctx, data: tuple) -> None:
    s = svc(context)
    record = await s.db.get_pull_request(int(data[1]))
    if record is None or record.number is None:
        raise ValidationError("Pull request not found.")
    await start_operation(update, context, "merge_pr", repo_ref(context, record.repo), {"record_id": record.id})


# ------------------------------------------------------------------ settings
SETTING_CHOICES = {
    "pr_auto_merge": ("Auto merge", [False, True]),
    "pr_merge_method": ("Merge method", list(MERGE_METHODS)),
    "pr_delete_branch": ("Delete branch after merge", [True, False]),
    "pr_require_checks": ("Require successful checks", [True, False]),
    "pr_max_concurrent": ("Maximum concurrent PR operations", [1, 3, 5, 10]),
}


async def show_settings(update: Update, context: Ctx) -> None:
    s = svc(context)
    settings = await s.pulls.settings()
    lines = [f"⚙️ <b>PR Automation settings</b> · account {code(s.active)}", ""]
    rows = []
    for key, (label, _) in SETTING_CHOICES.items():
        value = settings[key]
        shown = ("ON" if value else "OFF") if isinstance(value, bool) else value
        lines.append(f"{h(label)}: <b>{h(shown)}</b>")
        rows.append([btn(f"🔁 {label}", "pr_set", key)])
    allowed = settings["pr_allowed_repos"]
    lines += ["", "Allowed repositories: " + (", ".join(h(a.split('/')[-1]) for a in allowed) if allowed
                                              else "<i>all public repositories you own</i>")]
    if settings["pr_auto_merge"]:
        lines += ["", "⚠️ Auto-merge is ON: a pull request you confirm is merged automatically once every "
                  "required check and review passes. Branch protection still applies."]
    rows.append([btn("✏️ Allowed repositories", "pr_allow"), btn("🧹 Clear list", "pr_allow_clear")])
    rows.append([btn("⬅️ PR menu", "pr_menu")])
    await reply(update, context, join_limited(lines), kb(rows))


async def cmd_pr_settings(update: Update, context: Ctx) -> None:
    await show_settings(update, context)


@callback("pr_settings")
async def cb_settings(update: Update, context: Ctx, data: tuple) -> None:
    await show_settings(update, context)


@callback("pr_set")
async def cb_set(update: Update, context: Ctx, data: tuple) -> None:
    key = data[1]
    if key not in SETTING_CHOICES:
        return
    s = svc(context)
    choices = SETTING_CHOICES[key][1]
    current = (await s.pulls.settings())[key]
    index = choices.index(current) if current in choices else -1
    await s.pulls.set_setting(key, choices[(index + 1) % len(choices)])
    await show_settings(update, context)


@callback("pr_allow")
async def cb_allow(update: Update, context: Ctx, data: tuple) -> None:
    await_input(context, "pr_allowed")
    await reply(update, context, "✏️ Send the repository names allowed for PR automation, separated by spaces "
                                 "or commas. Send <code>-</code> to allow all your public repositories.")


@text_input("pr_allowed")
async def input_allowed(update: Update, context: Ctx, state: dict, text: str) -> None:
    s = svc(context)
    clear_input(context)
    if text.strip() == "-":
        await s.pulls.set_setting("pr_allowed_repos", [])
    else:
        names = [repo_ref(context, name).full_name for name in text.replace(",", " ").split() if name.strip()]
        if not names:
            raise ValidationError("Send at least one repository name, or '-' for all.")
        await s.pulls.set_setting("pr_allowed_repos", names)
    await show_settings(update, context)


@callback("pr_allow_clear")
async def cb_allow_clear(update: Update, context: Ctx, data: tuple) -> None:
    await svc(context).pulls.set_setting("pr_allowed_repos", [])
    await show_settings(update, context)


async def cmd_pr_auto(update: Update, context: Ctx) -> None:
    """Toggle auto-merge, or set it explicitly with /pr_auto on|off."""
    s = svc(context)
    settings = await s.pulls.settings()
    current = bool(settings["pr_auto_merge"])
    if context.args:
        word = context.args[0].lower()
        if word not in ("on", "off"):
            raise ValidationError("Usage: /pr_auto on|off")
        value = word == "on"
    else:
        value = not current
    await s.pulls.set_setting("pr_auto_merge", value)
    note = ("Pull requests you confirm will be merged automatically once every required check and review passes. "
            "Branch protection and required reviews are still enforced by GitHub."
            if value else "Merges now always need your confirmation.")
    await reply(update, context, f"🤖 Auto-merge is now <b>{'ON' if value else 'OFF'}</b>.\n{note}",
                kb([[btn("⚙️ PR settings", "pr_settings")]]), edit=False)


__all__ = ["PR_DEFAULTS", "cmd_pr", "cmd_pr_auto", "cmd_pr_create", "cmd_pr_history", "cmd_pr_list",
           "cmd_pr_settings", "cmd_pr_status", "show_menu"]
