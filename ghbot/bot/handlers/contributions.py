"""/contributions: which repositories keep each profile year tab alive, and how to clear them."""

from __future__ import annotations

from telegram import Update

from ghbot.bot.handlers.multianalyze import start_batch, start_delete_batch
from ghbot.bot.router import callback
from ghbot.bot.ui import Ctx, ProgressMessage, btn, code, h, join_limited, kb, reply, svc
from ghbot.services.contributions import contributions_by_year, empty_years, repos_for_years
from ghbot.validators import ValidationError

FIRST_YEAR = 2008


async def show_contributions(update: Update, context: Ctx, first_year: int | None = None) -> None:
    s = svc(context)
    progress = await ProgressMessage.create(update, context, "📊 Reading contributions per year (read-only)…")
    user = await s.gh.get_user()
    created = user["created_at"][:10]
    created_year = int(created[:4])
    current_year = int(user["updated_at"][:4]) if user.get("updated_at") else created_year
    start = first_year or FIRST_YEAR
    years = list(range(start, max(current_year, created_year) + 1))
    entries = await contributions_by_year(s.gh, s.username, years, progress)

    old = [e for e in entries if e.year < created_year]
    culprits = repos_for_years(entries, created_year, s.username)
    blank = empty_years(entries, created_year)

    lines = [f"📊 <b>Contributions per year</b> · {h(s.username)}", f"Account created: {h(created)}", ""]
    for entry in entries:
        if not entry.has_any and entry.commits == 0:
            continue
        mark = "⚠️ " if entry.year < created_year else ""
        top = ", ".join(f"{h(name.split('/', 1)[1])} ({count})" for name, count in entry.repos[:4])
        extra = f" +{len(entry.repos) - 4} more" if len(entry.repos) > 4 else ""
        lines.append(f"{mark}<b>{entry.year}</b>: {entry.commits} commits" + (f" · {top}{extra}" if top else ""))
    lines.append("")
    if old:
        lines += [
            f"⚠️ <b>{len(old)} year tab(s) older than your account</b>: {', '.join(str(e.year) for e in old)}",
            f"They come from {len(culprits)} repositories you own.",
            "⚠️ <b>Rewriting history does not clear these tabs</b>: GitHub keeps contributions recorded at push time, "
            "and the rewritten commits are counted again in the years you keep. Only deleting (or making private) "
            "the repository removes its contributions.", "",
        ]
    if blank:
        lines += [f"ℹ️ {', '.join(str(y) for y in blank)}: tab(s) with <b>0 contributions</b>. Nothing to remove; "
                  "these are empty tabs GitHub still renders.", ""]
    lines.append("<i>Read-only. Contributions counted here are commits authored by your account in repositories you own.</i>")

    rows = []
    if culprits:
        context.user_data["preset_repos"] = culprits  # type: ignore[index]
        rows.append([btn(f"🧹 Clean {len(culprits)} repos before {created}", "mb_preset", created)])
        rows.append([btn("📅 Clean those repos, my own date", "mb_preset", "")])
        rows.append([btn(f"🗑 Delete those {len(culprits)} repositories", "del_preset", created)])
    rows.append([btn("📅 Commit years", "years"), btn("🔄 Refresh", "contribs")])
    await progress.finish(context, update, join_limited(lines), kb(rows))


async def cmd_contributions(update: Update, context: Ctx) -> None:
    first = None
    if context.args:
        if not context.args[0].isdigit() or not 2005 <= int(context.args[0]) <= 2100:
            raise ValidationError("Usage: /contributions [start-year]")
        first = int(context.args[0])
    await show_contributions(update, context, first)


@callback("contribs")
async def cb_contributions(update: Update, context: Ctx, data: tuple) -> None:
    await show_contributions(update, context)


@callback("mb_preset")
async def cb_preset(update: Update, context: Ctx, data: tuple) -> None:
    preset = context.user_data.get("preset_repos")  # type: ignore[union-attr]
    if not preset:
        await reply(update, context, "That selection expired. Run /contributions again.")
        return
    await start_batch(update, context, cutoff=data[1] or None, preselected=list(preset))
    await reply(update, context, f"Preselected {len(preset)} repositories: {code(', '.join(n.split('/', 1)[1] for n in preset))}",
                edit=False)


@callback("del_preset")
async def cb_delete_preset(update: Update, context: Ctx, data: tuple) -> None:
    preset = context.user_data.get("preset_repos")  # type: ignore[union-attr]
    if not preset:
        await reply(update, context, "That selection expired. Run /contributions again.")
        return
    await start_delete_batch(update, context, list(preset),
                             f"These repositories hold the contributions for year tabs before {data[1]}.")
