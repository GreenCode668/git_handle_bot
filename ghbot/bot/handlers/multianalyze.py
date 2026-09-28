"""Analyze old commits across all or selected public repositories, then clean the chosen ones."""

from __future__ import annotations

import secrets
from typing import Any

from telegram import Update

from ghbot.bot.router import await_input, callback, clear_input, text_input
from ghbot.bot.ui import Ctx, ProgressMessage, btn, code, h, join_limited, kb, pager, reply, svc
from ghbot.services.operations import DEFAULT_SQUASH_STYLE
from ghbot.validators import RepoRef, ValidationError, parse_date

PER_PAGE = 10


def _batch(context: Ctx, batch_id: str | None = None) -> dict[str, Any]:
    batch = context.user_data.get("mbatch")  # type: ignore[union-attr]
    if not batch or (batch_id is not None and batch["id"] != batch_id):
        raise ValidationError("This multi-repository analysis expired. Start again with /analyze_many.")
    return batch


async def start_delete_batch(update: Update, context: Ctx, repos: list[str], note: str) -> None:
    """Prepare deletion of several repositories (each keeps its own backup and DELETE phrase)."""
    batch_id = secrets.token_hex(4)
    batch = {"id": batch_id, "selected": list(repos), "repos": list(repos), "cutoff": None,
             "items": [], "picked": [], "mode": "delete"}
    context.user_data["mbatch"] = batch  # type: ignore[index]
    names = ", ".join(n.split("/", 1)[1] for n in repos[:30]) + (" …" if len(repos) > 30 else "")
    await reply(update, context,
                f"🗑 <b>Delete {len(repos)} repositories?</b>\n{h(note)}\n\n{h(names)}\n\n"
                "Each repository is analyzed and backed up first, then you confirm each one by typing its "
                "DELETE phrase. Backups restore code, branches and tags only: issues, pull requests, releases "
                "and stars cannot be restored.\n\nNothing is changed by this step.",
                kb([[btn("▶️ Check and prepare", "mb_run", batch_id)], [btn("✖️ Cancel", "mb_cancel", batch_id)]]))


async def start_batch(update: Update, context: Ctx, cutoff: str | None = None,
                      preselected: list[str] | None = None) -> None:
    batch_id = secrets.token_hex(4)
    batch = {"id": batch_id, "selected": list(preselected or []), "repos": list(preselected or []),
             "cutoff": cutoff, "items": [], "picked": [], "mode": "history"}
    context.user_data["mbatch"] = batch  # type: ignore[index]
    if preselected:
        # Repositories chosen for you (e.g. from /contributions): straight to the date/confirmation step.
        await ask_date(update, context, batch)
        return
    await reply(update, context,
                "🔬 <b>Analyze old commits in many repositories</b>\n\n"
                "Choose which public repositories to analyze. Analysis is read-only. Afterwards you pick the "
                "repositories to clean; each one still gets its own verified backup and typed confirmation.\n"
                "Repositories are never deleted.",
                kb([[btn("📚 All public repositories", "mb_scope", batch_id, "all")],
                    [btn("☑️ Select repositories", "mb_scope", batch_id, "select")],
                    [btn("✖️ Cancel", "mb_cancel", batch_id)]]))


async def cmd_analyze_many(update: Update, context: Ctx) -> None:
    cutoff = parse_date(context.args[0]).isoformat() if context.args else None
    await start_batch(update, context, cutoff)


@callback("mb_start")
async def cb_start(update: Update, context: Ctx, data: tuple) -> None:
    cutoff = parse_date(data[1]).isoformat() if len(data) > 1 else None
    await start_batch(update, context, cutoff)


@callback("mb_scope")
async def cb_scope(update: Update, context: Ctx, data: tuple) -> None:
    batch = _batch(context, data[1])
    s = svc(context)
    batch["repos"] = [r["full_name"] for r in await s.bulk.managed_repos()]
    if not batch["repos"]:
        await reply(update, context, "No public repositories found.")
        return
    if data[2] == "all":
        batch["selected"] = list(batch["repos"])
        await ask_date(update, context, batch)
    else:
        favorites = {f.lower() for f in await s.db.list_favorites()}
        batch["repos"].sort(key=lambda n: (n.lower() not in favorites, n.lower()))
        await show_selection(update, context, batch, 1)


async def show_selection(update: Update, context: Ctx, batch: dict[str, Any], page: int) -> None:
    repos = batch["repos"]
    last = max(1, -(-len(repos) // PER_PAGE))
    page = min(max(1, page), last)
    chunk = repos[(page - 1) * PER_PAGE: page * PER_PAGE]
    selected = set(batch["selected"])
    rows = [[btn(("☑️ " if name in selected else "⬜ ") + name.split("/", 1)[1], "mb_tog", batch["id"], name, page)]
            for name in chunk]
    rows.append([btn("☑️ Select page", "mb_page", batch["id"], page, True), btn("⬜ Clear page", "mb_page", batch["id"], page, False)])
    rows.append(pager("mb_sel", page, page < last, batch["id"], last_page=last))
    rows.append([btn(f"✅ Done ({len(selected)} selected)", "mb_done", batch["id"]), btn("✖️ Cancel", "mb_cancel", batch["id"])])
    await reply(update, context, f"☑️ <b>Select repositories</b> ({len(selected)} of {len(repos)} selected)\n"
                                 "Tap to select or deselect. Favorites are listed first.", kb(rows))


@callback("mb_sel")
async def cb_sel(update: Update, context: Ctx, data: tuple) -> None:
    await show_selection(update, context, _batch(context, data[1]), int(data[2]))


@callback("mb_tog")
async def cb_tog(update: Update, context: Ctx, data: tuple) -> None:
    batch = _batch(context, data[1])
    if data[2] in batch["repos"]:
        if data[2] in batch["selected"]:
            batch["selected"].remove(data[2])
        else:
            batch["selected"].append(data[2])
    await show_selection(update, context, batch, int(data[3]))


@callback("mb_page")
async def cb_page(update: Update, context: Ctx, data: tuple) -> None:
    batch = _batch(context, data[1])
    page = int(data[2])
    chunk = batch["repos"][(page - 1) * PER_PAGE: page * PER_PAGE]
    selected = set(batch["selected"])
    selected = selected | set(chunk) if data[3] else selected - set(chunk)
    batch["selected"] = [n for n in batch["repos"] if n in selected]
    await show_selection(update, context, batch, page)


@callback("mb_done")
async def cb_done(update: Update, context: Ctx, data: tuple) -> None:
    batch = _batch(context, data[1])
    if not batch["selected"]:
        raise ValidationError("Select at least one repository.")
    await ask_date(update, context, batch)


async def ask_date(update: Update, context: Ctx, batch: dict[str, Any], force: bool = False) -> None:
    if batch["cutoff"] and not force:
        await confirm_analysis(update, context, batch)
        return
    await_input(context, "mbatch_date", batch_id=batch["id"])
    await reply(update, context, f"📅 Send the cutoff date for {len(batch['selected'])} repositories, e.g. <code>2013-01-01</code>.\n"
                                 "Commits dated before it will be analyzed. /cancel to abort.")


@callback("mb_date")
async def cb_change_date(update: Update, context: Ctx, data: tuple) -> None:
    batch = _batch(context, data[1])
    batch["cutoff"] = None
    batch["items"] = []
    batch["picked"] = []
    await ask_date(update, context, batch, force=True)


@text_input("mbatch_date")
async def input_date(update: Update, context: Ctx, state: dict, text: str) -> None:
    batch = _batch(context, state["batch_id"])
    batch["cutoff"] = parse_date(text).isoformat()
    clear_input(context)
    await confirm_analysis(update, context, batch)


async def confirm_analysis(update: Update, context: Ctx, batch: dict[str, Any]) -> None:
    names = ", ".join(n.split("/", 1)[1] for n in batch["selected"][:30]) + (" …" if len(batch["selected"]) > 30 else "")
    await reply(update, context,
                f"🔬 Analyze <b>{len(batch['selected'])}</b> repositories for commits before <b>{h(batch['cutoff'])}</b>?\n"
                f"{h(names)}\n\nRead-only: each repository is cloned temporarily and analyzed. Nothing is changed.",
                kb([[btn("▶️ Start analysis", "mb_run", batch["id"])],
                    [btn("📅 Change date", "mb_date", batch["id"]), btn("✖️ Cancel", "mb_cancel", batch["id"])]]))


@callback("mb_run")
async def cb_run(update: Update, context: Ctx, data: tuple) -> None:
    batch = _batch(context, data[1])
    if batch["items"]:
        await show_results(update, context, batch)
        return
    s = svc(context)
    owner = s.username
    repos = [RepoRef(owner, n.split("/", 1)[1]) for n in batch["selected"]]
    if batch.get("mode") == "delete":
        progress = await ProgressMessage.create(update, context, "🗑 Preparing deletions…")
        items = await s.batch.analyze_deletions(repos, progress)
    else:
        progress = await ProgressMessage.create(update, context, "🔬 Starting analysis…")
        params = {"cutoff": batch["cutoff"], "tag_policy": "snapshot", "squash_style": DEFAULT_SQUASH_STYLE}
        items = await s.batch.analyze(repos, params, progress)
    batch["items"] = [item.__dict__ for item in items]
    batch["picked"] = [item.op_id for item in items if item.status in ("rewrite", "delete")]
    await progress.finish(context, update, *results_view(batch))


def results_view(batch: dict[str, Any]):
    items = batch["items"]
    picked = set(batch["picked"])
    rewrite = [i for i in items if i["status"] == "rewrite"]
    deletions = [i for i in items if i["status"] == "delete"]
    actionable = rewrite + deletions
    if batch.get("mode") == "delete":
        lines = [f"🗑 <b>Repositories to delete</b>: {len(deletions)} of {len(items)} ready", ""]
    else:
        lines = [f"🔬 <b>Analysis before {h(batch['cutoff'])}</b>: {len(items)} repositories", ""]
    if rewrite:
        lines.append(f"<b>🧹 Old commits removed, repository kept ({len(rewrite)})</b>")
        for i in rewrite:
            lines.append(f"{'☑️' if i['op_id'] in picked else '⬜'} {h(i['repo'].split('/', 1)[1])}: {i['total']} commits · "
                         f"{i['removed']} removed → {i['kept']} kept")
    if deletions:
        header = ("<b>🗑 Will be DELETED</b>" if batch.get("mode") == "delete"
                  else f"<b>🗑 Nothing would remain: repository DELETED ({len(deletions)})</b>")
        lines += ["", header]
        for i in deletions:
            detail = i["detail"] or (f"all {i['total']} commits are older than the cutoff" if i["total"] else "")
            lines.append(f"{'☑️' if i['op_id'] in picked else '⬜'} {h(i['repo'].split('/', 1)[1])}"
                         + (f": {h(detail)}" if detail else ""))
    not_needed = [i for i in items if i["status"] == "not_needed"]
    if not_needed:
        lines += ["", f"<b>✅ Nothing to remove ({len(not_needed)})</b>",
                  ", ".join(h(i["repo"].split("/", 1)[1]) for i in not_needed)]
    problems = [i for i in items if i["status"] in ("blocked", "error")]
    if problems:
        lines += ["", f"<b>🛑 Blocked ({len(problems)})</b>"]
        lines += [f"• {h(i['repo'].split('/', 1)[1])}: {h(i['detail'][:150])}" for i in problems]
    lines += ["", "Nothing has been changed. Tap a repository to include or exclude it, 🔎 for full details."]
    if deletions:
        lines.append("🗑 Deleting a repository also removes its issues, pull requests, releases and stars, which a git "
                     "backup cannot restore.")
    rows = [[btn(("☑️ " if i["op_id"] in picked else "⬜ ") + ("🗑 " if i["status"] == "delete" else "")
                 + i["repo"].split("/", 1)[1], "mb_pick", batch["id"], i["op_id"]),
             btn("🔎", "op", i["op_id"])] for i in actionable[:40]]
    if actionable:
        rows.append([btn(f"💾 Continue: backup {len(picked)} selected", "mb_approve_ask", batch["id"])])
    rows.append([btn("✖️ Cancel all", "mb_cancel", batch["id"])])
    return join_limited(lines), kb(rows)


async def show_results(update: Update, context: Ctx, batch: dict[str, Any]) -> None:
    await reply(update, context, *results_view(batch))


@callback("mb_pick")
async def cb_pick(update: Update, context: Ctx, data: tuple) -> None:
    batch = _batch(context, data[1])
    op_id = int(data[2])
    if op_id in batch["picked"]:
        batch["picked"].remove(op_id)
    elif any(i["op_id"] == op_id and i["status"] in ("rewrite", "delete") for i in batch["items"]):
        batch["picked"].append(op_id)
    await show_results(update, context, batch)


@callback("mb_approve_ask")
async def cb_approve_ask(update: Update, context: Ctx, data: tuple) -> None:
    batch = _batch(context, data[1])
    if not batch["picked"]:
        raise ValidationError("Select at least one repository.")
    chosen = [i for i in batch["items"] if i["op_id"] in batch["picked"]]
    names = [i["repo"] for i in chosen]
    to_delete = [i["repo"].split("/", 1)[1] for i in chosen if i["status"] == "delete"]
    await reply(update, context,
                f"💾 Create and verify a full mirror backup of <b>{len(names)}</b> repositories and lock them?\n"
                f"{h(', '.join(n.split('/', 1)[1] for n in names))}\n"
                + (f"\n🗑 To be DELETED afterwards: {h(', '.join(to_delete))}\n" if to_delete else "") + "\n"
                "Nothing is rewritten in this step. Unselected repositories are cancelled. After the backups you "
                "confirm each repository separately by typing its REWRITE phrase.",
                kb([[btn("💾 Create backups", "mb_approve", batch["id"]), btn("⬅️ Back", "mb_run", batch["id"])]]))


@callback("mb_approve")
async def cb_approve(update: Update, context: Ctx, data: tuple) -> None:
    batch = _batch(context, data[1])
    s = svc(context)
    all_ops = [i["op_id"] for i in batch["items"] if i["status"] in ("rewrite", "delete")]
    picked = [op_id for op_id in all_ops if op_id in batch["picked"]]
    await s.batch.cancel([op_id for op_id in all_ops if op_id not in picked])
    progress = await ProgressMessage.create(update, context, "💾 Creating backups…")
    results = await s.batch.approve(picked, progress)
    context.user_data.pop("mbatch", None)  # type: ignore[union-attr]
    ready = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]
    lines = [f"💾 <b>Backups finished</b>: {len(ready)} ready · {len(failed)} stopped", ""]
    if ready:
        minutes = int(await s.db.get_setting("confirmation_ttl_minutes", 30))
        lines.append(f"<b>Ready: send each phrase as its own message within {minutes} minutes</b>")
        lines.append("<i>REWRITE removes the old commits · DELETE removes the whole repository</i>")
        for r in ready:
            lines.append(f"• {h(r.repo.split('/', 1)[1])}: backup {code(r.backup_id)}\n  {code(r.phrase)}")
        lines.append(f"\n⏳ Each confirmation expires {minutes} minutes after its backup finished; expired ones change "
                     "nothing and can be restarted. Raise the timeout in ⚙️ Settings. Repeating the flow reuses these "
                     "verified backups while GitHub is unchanged, so it is fast. See /pending.")
    if failed:
        lines += ["", "<b>🛑 Stopped (nothing changed)</b>"] + [f"• {h(r.repo)}: {h(r.detail[:200])}" for r in failed]
    await progress.finish(context, update, join_limited(lines), kb([[btn("⏳ Pending", "pending")]]))


@callback("mb_cancel")
async def cb_cancel(update: Update, context: Ctx, data: tuple) -> None:
    batch = context.user_data.pop("mbatch", None)  # type: ignore[union-attr]
    clear_input(context)
    cancelled = 0
    if batch and batch["id"] == data[1]:
        cancelled = await svc(context).batch.cancel([i["op_id"] for i in batch["items"] if i["op_id"]])
    await reply(update, context, f"✖️ Multi-repository analysis cancelled ({cancelled} pending analyses closed). Nothing was changed.")
