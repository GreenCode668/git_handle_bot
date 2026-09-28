"""GitHub Actions: workflows, runs, failed runs, rerun and cancel (through the safety engine)."""

from __future__ import annotations

from telegram import Update

from ghbot.bot.handlers.operations import start_operation
from ghbot.bot.handlers.repos import readable, repo_ref, show_repo_list
from ghbot.bot.router import callback
from ghbot.bot.ui import Ctx, ago, btn, code, h, join_limited, kb, pager, reply, run_emoji, svc, url_btn
from ghbot.validators import RepoRef, ValidationError, validate_run_id

FILTERS = {"all": None, "in_progress": "in_progress", "queued": "queued", "success": "success", "failure": "failure"}
FILTER_LABELS = {"all": "All", "in_progress": "⏳ Running", "queued": "🕒 Queued", "success": "✅ Success", "failure": "❌ Failed"}
RUN_ACTIONS = {"rerun": ("actions_rerun", "🔁 Re-run all jobs"), "rerun_failed": ("actions_rerun_failed", "🔁 Re-run failed jobs"),
               "cancel": ("actions_cancel", "🛑 Cancel run")}


async def show_actions(update: Update, context: Ctx, repo: RepoRef, status: str, page: int) -> None:
    repo, _ = await readable(context, repo.full_name)
    s = svc(context)
    status = status if status in FILTERS else "all"
    workflows = await s.gh.list_workflows(repo.full_name) if page == 1 and status == "all" else []
    runs = await s.gh.list_runs(repo.full_name, page=page, per_page=8, status=FILTERS[status])
    lines = [f"⚙️ <b>GitHub Actions</b> · {code(repo)} · {FILTER_LABELS[status]}", ""]
    if page == 1 and status == "all":
        lines.append(f"<b>Workflows ({len(workflows)})</b>")
        lines += [f"• {h(w['name'])} — {h(w['state'])}" for w in workflows[:10]] or ["• none"]
        lines.append("")
    lines.append("<b>Runs</b>")
    rows = []
    for run in runs.items:
        lines.append(f"{run_emoji(run)} #{run['run_number']} {h(run['name'])} · {h(run.get('head_branch'))} · "
                     f"{h(run.get('event'))} · {ago(run.get('created_at'))} · id {code(run['id'])}")
        rows.append([btn(f"{run_emoji(run)} #{run['run_number']} {run['name']}"[:60], "run", repo.full_name, run["id"])])
    if not runs.items:
        lines.append("No runs.")
    rows.append([btn(("• " if k == status else "") + v, "actions", repo.full_name, k, 1) for k, v in list(FILTER_LABELS.items())[:3]])
    rows.append([btn(("• " if k == status else "") + v, "actions", repo.full_name, k, 1) for k, v in list(FILTER_LABELS.items())[3:]])
    rows.append(pager("actions", page, runs.has_next, repo.full_name, status))
    rows.append([btn("📜 Workflows", "workflows", repo.full_name), btn("⬅️ Repository", "pick", "detail", repo.full_name)])
    await reply(update, context, join_limited(lines), kb(rows))


@callback("actions")
async def cb_actions(update: Update, context: Ctx, data: tuple) -> None:
    await show_actions(update, context, repo_ref(context, data[1]), data[2], int(data[3]))


async def show_workflows(update: Update, context: Ctx, text: str) -> None:
    repo, _ = await readable(context, text)
    workflows = await svc(context).gh.list_workflows(repo.full_name)
    state_emoji = {"active": "🟢", "disabled_manually": "⏸", "disabled_inactivity": "💤"}
    lines = [f"📜 <b>Workflows · {h(repo.full_name)}</b>", ""]
    for w in workflows:
        lines.append(f"{state_emoji.get(w['state'], '⚪')} <b>{h(w['name'])}</b> — {h(w['state'])}\n    {code(w['path'])} · id {code(w['id'])}")
    if not workflows:
        lines.append("No workflows.")
    await reply(update, context, join_limited(lines),
                kb([[btn("▶️ Runs", "actions", repo.full_name, "all", 1), btn("⬅️ Repository", "pick", "detail", repo.full_name)]]))


@callback("workflows")
async def cb_workflows(update: Update, context: Ctx, data: tuple) -> None:
    await show_workflows(update, context, data[1])


def available_actions(run: dict) -> list[str]:
    if run["status"] in ("in_progress", "queued", "waiting", "requested", "pending"):
        return ["cancel"]
    actions = ["rerun"]
    if run.get("conclusion") in ("failure", "cancelled", "timed_out"):
        actions.append("rerun_failed")
    return actions


@callback("run")
async def cb_run(update: Update, context: Ctx, data: tuple) -> None:
    repo, meta = await readable(context, data[1])
    run = await svc(context).gh.get_run(repo.full_name, validate_run_id(data[2]))
    lines = [
        f"{run_emoji(run)} <b>{h(run['name'])}</b> #{run['run_number']} · id {code(run['id'])}",
        f"Status: {h(run['status'])} · Conclusion: {h(run.get('conclusion') or '—')}",
        f"Branch: {code(run.get('head_branch'))} · Commit: {code((run.get('head_sha') or '')[:7])}",
        f"Event: {h(run.get('event'))} · Attempt: {run.get('run_attempt', 1)}",
        f"Started: {ago(run.get('run_started_at'))} · Updated: {ago(run.get('updated_at'))}",
        f"Title: {h((run.get('display_title') or '')[:120])}",
    ]
    rows = [] if meta["private"] else [[btn(RUN_ACTIONS[a][1], "run_ask", repo.full_name, run["id"], a)] for a in available_actions(run)]
    rows.append([url_btn("🔗 Open on GitHub", run["html_url"]), btn("🔄 Refresh", "run", repo.full_name, run["id"])])
    rows.append([btn("⬅️ Runs", "actions", repo.full_name, "all", 1)])
    await reply(update, context, "\n".join(lines), kb(rows))


@callback("run_ask")
async def cb_run_ask(update: Update, context: Ctx, data: tuple) -> None:
    if data[3] not in RUN_ACTIONS:
        return
    await start_operation(update, context, RUN_ACTIONS[data[3]][0], repo_ref(context, data[1]),
                          {"run_id": validate_run_id(data[2])})


async def _repo_arg(update: Update, context: Ctx, purpose_usage: str) -> RepoRef | None:
    if not context.args:
        await show_repo_list(update, context, "actions", 1)
        return None
    return repo_ref(context, context.args[0])


async def cmd_actions(update: Update, context: Ctx) -> None:
    repo = await _repo_arg(update, context, "/actions repo")
    if repo:
        await show_actions(update, context, repo, "all", 1)


async def cmd_runs(update: Update, context: Ctx) -> None:
    repo = await _repo_arg(update, context, "/runs repo")
    if repo:
        await show_actions(update, context, repo, "all", 1)


async def cmd_failed(update: Update, context: Ctx) -> None:
    repo = await _repo_arg(update, context, "/failed repo")
    if repo:
        await show_actions(update, context, repo, "failure", 1)


async def cmd_workflows(update: Update, context: Ctx) -> None:
    if not context.args:
        raise ValidationError("Usage: /workflows repo")
    await show_workflows(update, context, context.args[0])


async def _run_command(update: Update, context: Ctx, usage: str, choose) -> None:
    args = context.args or []
    if len(args) != 2:
        raise ValidationError(f"Usage: {usage}")
    repo, _ = await readable(context, args[0])
    run_id = validate_run_id(args[1])
    run = await svc(context).gh.get_run(repo.full_name, run_id)
    await start_operation(update, context, choose(run), repo, {"run_id": run_id})


async def cmd_rerun(update: Update, context: Ctx) -> None:
    await _run_command(update, context, "/rerun repo run-id", lambda run: "actions_rerun")


async def cmd_cancel_run(update: Update, context: Ctx) -> None:
    await _run_command(update, context, "/cancel_run repo run-id", lambda run: "actions_cancel")
