"""Repository information commands and small metadata edits (all edits go through the safety engine)."""

from __future__ import annotations

import asyncio

from telegram import Update

from ghbot.bot.handlers.operations import start_operation
from ghbot.bot.handlers.repos import readable, repo_flags, repo_ref, show_repo_list
from ghbot.bot.router import await_input, callback, clear_input, text_input
from ghbot.bot.ui import Ctx, ProgressMessage, ago, btn, code, fmt_bytes, h, join_limited, kb, pager, reply, svc
from ghbot.services.years import collect_year_spans, year_report
from ghbot.github.client import GitHubError
from ghbot.validators import RepoRef, ValidationError, validate_repo_name, validate_search, validate_topic


def _need_repo(context: Ctx, usage: str) -> str:
    if not context.args:
        raise ValidationError(f"Usage: {usage}")
    return context.args[0]


def _back(repo: RepoRef) -> list:
    return [btn("⬅️ Repository", "pick", "detail", repo.full_name), btn("➕ More", "repo_more", repo.full_name)]


# ------------------------------------------------------------------ more menu
@callback("repo_more")
async def cb_more(update: Update, context: Ctx, data: tuple) -> None:
    repo, meta = await readable(context, data[1])
    name = repo.full_name
    is_fav = name.lower() in {f.lower() for f in await svc(context).db.list_favorites()}
    protected = await svc(context).db.get_protection(name) is not None
    rows = [
        [btn("📈 Stats", "ri_stats", name), btn("🈯 Languages", "ri_langs", name), btn("👥 Contributors", "contributors", name, 1)],
        [btn("🏷 Tags", "ri_tags", name, 1), btn("📦 Releases", "ri_releases", name, 1), btn("📋 Clone URL", "ri_clone", name)],
        [btn("🐞 Issues", "ri_issues", name, 1), btn("🔃 Pull requests", "ri_pulls", name, 1), btn("📊 History stats", "hist_stats", name)],
        [btn("📝 Description", "ri_desc", name), btn("🔗 Homepage", "ri_home", name), btn("#️⃣ Topics", "ri_topics", name)],
        [btn("💾 Backups", "backup_repo", name, 1),
         btn("☆ Unfavorite" if is_fav else "⭐ Favorite", "fav_toggle", name),
         btn("🔓 Unlock" if protected else "🛡 Protect", "unlock_ask" if protected else "lock_do", name)],
        [btn("⬅️ Repository", "pick", "detail", name)],
    ]
    await reply(update, context, f"➕ <b>{h(name)}</b> {repo_flags(meta)}\nChoose:", kb(rows))


# -------------------------------------------------------------- recent/search
async def cmd_recent(update: Update, context: Ctx) -> None:
    await show_repo_list(update, context, "detail", 1, sort="updated")


@callback("search_help")
async def cb_search_help(update: Update, context: Ctx, data: tuple) -> None:
    await_input(context, "search")
    await reply(update, context, "🔎 Send text to search in repository names and descriptions. /cancel to abort.")


@text_input("search")
async def input_search(update: Update, context: Ctx, state: dict, text: str) -> None:
    clear_input(context)
    await run_search(update, context, text)


async def cmd_search(update: Update, context: Ctx) -> None:
    if not context.args:
        await cb_search_help(update, context, ())
        return
    await run_search(update, context, " ".join(context.args))


async def run_search(update: Update, context: Ctx, raw: str) -> None:
    query = validate_search(raw).lower()
    s = svc(context)
    repos = s.policy.filter_visible(await s.gh.all_repos(public_only=not s.policy.show_private))
    matches = [r for r in repos if query in r["name"].lower() or query in (r.get("description") or "").lower()]
    lines = [f"🔎 <b>Search</b> {code(raw.strip())}: {len(matches)} match(es)", ""]
    rows = []
    for r in matches[:20]:
        lines.append(f"{repo_flags(r)} <b>{h(r['name'])}</b> — {h((r.get('description') or '')[:80])}")
        rows.append([btn(f"{repo_flags(r)} {r['name']}", "pick", "detail", r["full_name"])])
    if len(matches) > 20:
        lines.append(f"… {len(matches) - 20} more, refine your search.")
    await reply(update, context, join_limited(lines), kb(rows) if rows else None)


# --------------------------------------------------------------------- create
@callback("create_help")
async def cb_create_help(update: Update, context: Ctx, data: tuple) -> None:
    await_input(context, "create_name")
    await reply(update, context, "➕ Send the name of the new <b>public</b> repository. /cancel to abort.")


@text_input("create_name")
async def input_create_name(update: Update, context: Ctx, state: dict, text: str) -> None:
    await ask_create_description(update, context, text)


async def cmd_create(update: Update, context: Ctx) -> None:
    if not context.args:
        await cb_create_help(update, context, ())
        return
    await ask_create_description(update, context, context.args[0])


async def ask_create_description(update: Update, context: Ctx, name: str) -> None:
    name = validate_repo_name(name)
    if await svc(context).gh.repo_exists(f"{svc(context).username}/{name}"):
        clear_input(context)
        raise ValidationError(f"A repository named {name} already exists.")
    await_input(context, "create_description", name=name)
    await reply(update, context, f"📝 Send a description for {code(name)} (max 350 chars), or <code>-</code> for none.",
                kb([[btn("Skip description", "create_skip", name)]]))


@text_input("create_description")
async def input_create_description(update: Update, context: Ctx, state: dict, text: str) -> None:
    clear_input(context)
    await start_operation(update, context, "create_repo", repo_ref(context, state["name"]),
                          {"description": "" if text.strip() == "-" else text.strip(), "private": False})


@callback("create_skip")
async def cb_create_skip(update: Update, context: Ctx, data: tuple) -> None:
    clear_input(context)
    await start_operation(update, context, "create_repo", repo_ref(context, data[1]), {"description": "", "private": False})


# ---------------------------------------------------------------- info views
async def cmd_clone(update: Update, context: Ctx) -> None:
    await show_clone(update, context, _need_repo(context, "/clone repo"))


@callback("ri_clone")
async def cb_clone(update: Update, context: Ctx, data: tuple) -> None:
    await show_clone(update, context, data[1])


async def show_clone(update: Update, context: Ctx, text: str) -> None:
    repo, meta = await readable(context, text)
    await reply(update, context, f"📋 <b>Clone {h(repo.full_name)}</b>\n\n<code>git clone {h(meta['clone_url'])}</code>",
                kb([_back(repo)]))


async def show_stats(update: Update, context: Ctx, repo_or_text: RepoRef | str) -> None:
    repo, meta = await readable(context, repo_or_text.full_name if isinstance(repo_or_text, RepoRef) else repo_or_text)
    gh = svc(context).gh

    async def safe(coro, default="?"):
        try:
            return await coro
        except GitHubError:
            return default

    commits, branches, pulls = await asyncio.gather(
        safe(gh.count_commits(repo.full_name), 0),
        safe(gh.count(f"/repos/{repo.full_name}/branches")),
        safe(gh.count_open_pulls(repo.full_name)),
    )
    issues = meta["open_issues_count"] - pulls if isinstance(pulls, int) else meta["open_issues_count"]
    lines = [
        f"📈 <b>Statistics · {h(repo.full_name)}</b> {repo_flags(meta)}", "",
        f"Commits (default branch): {commits}",
        f"Branches: {branches}",
        f"⭐ Stars: {meta['stargazers_count']} · 🍴 Forks: {meta['forks_count']} · 👀 Watchers: {meta.get('subscribers_count', meta.get('watchers_count'))}",
        f"🐞 Open issues: {issues} · 🔃 Open PRs: {pulls}",
        f"Size: {fmt_bytes((meta.get('size') or 0) * 1024)}",
        f"Language: {h(meta.get('language') or '—')}",
        f"License: {h((meta.get('license') or {}).get('spdx_id') or '—')}",
        f"Created: {ago(meta.get('created_at'))} · Updated: {ago(meta.get('updated_at'))} · Pushed: {ago(meta.get('pushed_at'))}",
    ]
    await reply(update, context, "\n".join(lines), kb([_back(repo)]))


async def cmd_stats(update: Update, context: Ctx) -> None:
    await show_stats(update, context, _need_repo(context, "/stats repo"))


@callback("ri_stats")
async def cb_stats(update: Update, context: Ctx, data: tuple) -> None:
    await show_stats(update, context, data[1])


async def show_languages(update: Update, context: Ctx, text: str) -> None:
    repo, _ = await readable(context, text)
    languages = await svc(context).gh.languages(repo.full_name)
    total = sum(languages.values()) or 1
    lines = [f"🈯 <b>Languages · {h(repo.full_name)}</b>", ""]
    for name, size in sorted(languages.items(), key=lambda kv: kv[1], reverse=True):
        pct = size * 100 / total
        bar = "█" * max(1, round(pct / 5))
        lines.append(f"{code(f'{pct:5.1f}%')} {bar} {h(name)}")
    if not languages:
        lines.append("No language data.")
    await reply(update, context, join_limited(lines), kb([_back(repo)]))


async def cmd_languages(update: Update, context: Ctx) -> None:
    await show_languages(update, context, _need_repo(context, "/languages repo"))


@callback("ri_langs")
async def cb_langs(update: Update, context: Ctx, data: tuple) -> None:
    await show_languages(update, context, data[1])


async def show_tags(update: Update, context: Ctx, text: str, page: int) -> None:
    repo, _ = await readable(context, text)
    result = await svc(context).gh.list_tags(repo.full_name, page=page)
    lines = [f"🏷 <b>Tags · {h(repo.full_name)}</b>", ""]
    lines += [f"{code(t['name'])} → {code(t['commit']['sha'][:7])}" for t in result.items] or ["No tags."]
    await reply(update, context, join_limited(lines),
                kb([pager("ri_tags", page, result.has_next, repo.full_name, last_page=result.last_page), _back(repo)]))


async def cmd_tags(update: Update, context: Ctx) -> None:
    await show_tags(update, context, _need_repo(context, "/tags repo"), 1)


@callback("ri_tags")
async def cb_tags(update: Update, context: Ctx, data: tuple) -> None:
    await show_tags(update, context, data[1], int(data[2]))


async def show_releases(update: Update, context: Ctx, text: str, page: int) -> None:
    repo, _ = await readable(context, text)
    result = await svc(context).gh.list_releases(repo.full_name, page=page)
    lines = [f"📦 <b>Releases · {h(repo.full_name)}</b>", ""]
    for r in result.items:
        flags = ("📝 draft " if r.get("draft") else "") + ("🧪 pre-release " if r.get("prerelease") else "")
        lines.append(f"<b>{h(r.get('name') or r['tag_name'])}</b> {code(r['tag_name'])} {flags}· {ago(r.get('published_at') or r.get('created_at'))}"
                     f" · assets {len(r.get('assets', []))}")
    if not result.items:
        lines.append("No releases.")
    await reply(update, context, join_limited(lines),
                kb([pager("ri_releases", page, result.has_next, repo.full_name, last_page=result.last_page), _back(repo)]))


async def cmd_releases(update: Update, context: Ctx) -> None:
    await show_releases(update, context, _need_repo(context, "/releases repo"), 1)


@callback("ri_releases")
async def cb_releases(update: Update, context: Ctx, data: tuple) -> None:
    await show_releases(update, context, data[1], int(data[2]))


async def show_issues(update: Update, context: Ctx, text: str, page: int, pulls: bool) -> None:
    repo, _ = await readable(context, text)
    gh = svc(context).gh
    result = await (gh.list_pulls if pulls else gh.list_issues)(repo.full_name, page=page)
    title = "🔃 Open pull requests" if pulls else "🐞 Open issues"
    lines = [f"<b>{title} · {h(repo.full_name)}</b>", ""]
    rows = []
    for item in result.items:
        labels = ", ".join(lbl["name"] for lbl in item.get("labels", [])[:3])
        extra = f" {code(item['head']['ref'])}→{code(item['base']['ref'])}" if pulls else (f" [{h(labels)}]" if labels else "")
        draft = " 📝draft" if item.get("draft") else ""
        lines.append(f"#{item['number']} {h(item['title'][:80])}{draft}{extra}\n    by {h(item['user']['login'])} · {ago(item['created_at'])}")
    if not result.items:
        lines.append("None open.")
    route = "ri_pulls" if pulls else "ri_issues"
    rows.append(pager(route, page, result.has_next, repo.full_name, last_page=result.last_page))
    rows.append(_back(repo))
    await reply(update, context, join_limited(lines), kb(rows))


async def cmd_issues(update: Update, context: Ctx) -> None:
    await show_issues(update, context, _need_repo(context, "/issues repo"), 1, pulls=False)


async def cmd_pulls(update: Update, context: Ctx) -> None:
    await show_issues(update, context, _need_repo(context, "/pulls repo"), 1, pulls=True)


@callback("ri_issues")
async def cb_issues(update: Update, context: Ctx, data: tuple) -> None:
    await show_issues(update, context, data[1], int(data[2]), pulls=False)


@callback("ri_pulls")
async def cb_pulls(update: Update, context: Ctx, data: tuple) -> None:
    await show_issues(update, context, data[1], int(data[2]), pulls=True)


# ------------------------------------------------- description / homepage / topics
FIELDS = {"description": ("📝 Description", "set_description", "max 350 characters"),
          "homepage": ("🔗 Homepage", "set_homepage", "full https:// URL")}


async def show_field(update: Update, context: Ctx, text: str, field: str) -> None:
    repo, meta = await readable(context, text)
    label, _, hint = FIELDS[field]
    rows = [] if meta["private"] else [[btn(f"✏️ Change {field}", "field_edit", repo.full_name, field),
                                         btn(f"🧹 Clear {field}", "field_clear", repo.full_name, field)]]
    rows.append(_back(repo))
    await reply(update, context, f"<b>{label} · {h(repo.full_name)}</b>\n\n{h(meta.get(field) or '—')}\n\n<i>{h(hint)}</i>", kb(rows))


async def _field_command(update: Update, context: Ctx, field: str) -> None:
    text = _need_repo(context, f"/{field} repo [new value]")
    if len(context.args) > 1:
        repo = repo_ref(context, text)
        await start_operation(update, context, FIELDS[field][1], repo, {"value": " ".join(context.args[1:])})
        return
    await show_field(update, context, text, field)


async def cmd_description(update: Update, context: Ctx) -> None:
    await _field_command(update, context, "description")


async def cmd_homepage(update: Update, context: Ctx) -> None:
    await _field_command(update, context, "homepage")


@callback("ri_desc")
async def cb_desc(update: Update, context: Ctx, data: tuple) -> None:
    await show_field(update, context, data[1], "description")


@callback("ri_home")
async def cb_home(update: Update, context: Ctx, data: tuple) -> None:
    await show_field(update, context, data[1], "homepage")


@callback("field_edit")
async def cb_field_edit(update: Update, context: Ctx, data: tuple) -> None:
    if data[2] not in FIELDS:
        return
    await_input(context, "repo_field", repo=data[1], field=data[2])
    await reply(update, context, f"✏️ Send the new {h(data[2])} for {code(data[1])} ({h(FIELDS[data[2]][2])}). /cancel to abort.")


@callback("field_clear")
async def cb_field_clear(update: Update, context: Ctx, data: tuple) -> None:
    if data[2] in FIELDS:
        await start_operation(update, context, FIELDS[data[2]][1], repo_ref(context, data[1]), {"value": ""})


@text_input("repo_field")
async def input_repo_field(update: Update, context: Ctx, state: dict, text: str) -> None:
    clear_input(context)
    await start_operation(update, context, FIELDS[state["field"]][1], repo_ref(context, state["repo"]), {"value": text})


async def show_topics(update: Update, context: Ctx, text: str) -> None:
    repo, meta = await readable(context, text)
    topics = sorted(meta.get("topics") or [])
    lines = [f"#️⃣ <b>Topics · {h(repo.full_name)}</b>", "", " ".join(code(t) for t in topics) or "No topics."]
    rows = []
    if not meta["private"]:
        rows.append([btn("➕ Add topics", "topic_add", repo.full_name)])
        rows += [[btn(f"➖ {t}", "topic_rm", repo.full_name, t) for t in topics[i:i + 3]] for i in range(0, len(topics), 3)]
    rows.append(_back(repo))
    await reply(update, context, "\n".join(lines), kb(rows))


async def cmd_topics(update: Update, context: Ctx) -> None:
    await show_topics(update, context, _need_repo(context, "/topics repo"))


@callback("ri_topics")
async def cb_topics(update: Update, context: Ctx, data: tuple) -> None:
    await show_topics(update, context, data[1])


@callback("topic_add")
async def cb_topic_add(update: Update, context: Ctx, data: tuple) -> None:
    await_input(context, "topics_add", repo=data[1])
    await reply(update, context, f"➕ Send topics to add to {code(data[1])}, separated by spaces or commas. /cancel to abort.")


@text_input("topics_add")
async def input_topics_add(update: Update, context: Ctx, state: dict, text: str) -> None:
    new = [validate_topic(t) for t in text.replace(",", " ").split() if t.strip()]
    if not new:
        raise ValidationError("Send at least one topic.")
    clear_input(context)
    repo, meta = await readable(context, state["repo"])
    await start_operation(update, context, "set_topics", repo, {"topics": sorted(set(meta.get("topics") or []) | set(new))})


@callback("topic_rm")
async def cb_topic_rm(update: Update, context: Ctx, data: tuple) -> None:
    repo, meta = await readable(context, data[1])
    remaining = sorted(set(meta.get("topics") or []) - {data[2]})
    await start_operation(update, context, "set_topics", repo, {"topics": remaining})


# ---------------------------------------------------------------- commit years
async def show_years(update: Update, context: Ctx) -> None:
    s = svc(context)
    progress = await ProgressMessage.create(update, context, "📅 Reading commit years (read-only)…")
    user = await s.gh.get_user()
    created = user["created_at"][:10]
    created_year = int(created[:4])
    repos = [r["full_name"] for r in s.policy.filter_visible(await s.gh.all_repos(public_only=not s.policy.show_private))]
    spans = await collect_year_spans(s.gh, repos, progress)
    report = year_report(spans, created_year)

    lines = [f"📅 <b>Commit years</b> · {len(repos)} repositories", f"Account created: {h(created)}", ""]
    if report["old_years"]:
        lines.append(f"<b>⚠️ Years older than your account: {', '.join(str(y) for y in report['old_years'])}</b>")
        lines.append("These repositories contain those commits and cause the old year tabs on your profile:")
        for repo, years in report["culprits"].items():
            span = next(x for x in spans if x.repo == repo)
            lines.append(f"• <b>{h(repo.split('/', 1)[1])}</b>: {span.first}–{span.last} "
                         f"({span.commits} commits) · old years {', '.join(str(y) for y in sorted(years))}")
        lines.append("")
    else:
        lines += ["✅ No repository has commits from before your account was created.", ""]
    lines.append("<b>All repositories (oldest commit first)</b>")
    for span in spans:
        if span.error:
            lines.append(f"• {h(span.repo.split('/', 1)[1])}: ⚠️ {h(span.error)}")
        elif span.first is None:
            lines.append(f"• {h(span.repo.split('/', 1)[1])}: empty")
        else:
            mark = "⚠️ " if span.first < created_year else ""
            lines.append(f"• {mark}{h(span.repo.split('/', 1)[1])}: {span.first}–{span.last} ({span.commits} commits)")
    lines += ["", "<i>Default branch only; other branches may reach further back (/history_stats repo covers all branches). "
              "Year tabs are not a setting: they disappear only when the old commits are gone, and GitHub may cache them.</i>"]
    rows = []
    if report["culprits"]:
        rows.append([btn(f"🔬 Analyze commits before {created}", "mb_start", created),
                     btn("📅 Own date", "mb_start")])
    rows.append([btn("📊 Contributions per year", "contribs"), btn("🔄 Refresh", "years")])
    await progress.finish(context, update, join_limited(lines), kb(rows))


async def cmd_years(update: Update, context: Ctx) -> None:
    await show_years(update, context)


@callback("years")
async def cb_years(update: Update, context: Ctx, data: tuple) -> None:
    await show_years(update, context)


# ------------------------------------------------------------------ favorites
async def cmd_favorite(update: Update, context: Ctx) -> None:
    repo, meta = await readable(context, _need_repo(context, "/favorite repo"))
    added = await svc(context).db.add_favorite(meta["full_name"])
    await reply(update, context, f"⭐ {code(meta['full_name'])} {'added to' if added else 'is already in'} favorites.", edit=False)


async def cmd_unfavorite(update: Update, context: Ctx) -> None:
    repo = repo_ref(context, _need_repo(context, "/unfavorite repo"))
    removed = await svc(context).db.remove_favorite(repo.full_name)
    await reply(update, context, f"☆ {code(repo.full_name)} {'removed from' if removed else 'was not in'} favorites.", edit=False)


@callback("fav_toggle")
async def cb_fav_toggle(update: Update, context: Ctx, data: tuple) -> None:
    repo, meta = await readable(context, data[1])
    db = svc(context).db
    if not await db.remove_favorite(meta["full_name"]):
        await db.add_favorite(meta["full_name"])
    await cb_more(update, context, ("repo_more", meta["full_name"]))


async def show_favorites(update: Update, context: Ctx) -> None:
    s = svc(context)
    favorites = await s.db.list_favorites()
    lines = ["⭐ <b>Favorite repositories</b>", ""]
    rows = []
    for full_name in favorites:
        try:
            meta = await s.policy.readable(RepoRef(*full_name.split("/", 1)))
        except Exception:  # noqa: BLE001 - hidden, renamed or deleted
            lines.append(f"⚠️ {h(full_name)} (not available)")
            rows.append([btn(f"☆ Remove {full_name.split('/', 1)[1]}", "fav_rm", full_name)])
            continue
        lines.append(f"{repo_flags(meta)} <b>{h(meta['name'])}</b> · pushed {ago(meta.get('pushed_at'))}")
        rows.append([btn(f"⭐ {meta['name']}", "pick", "detail", meta["full_name"])])
    if not favorites:
        lines.append("No favorites yet. Use /favorite repo or the ➕ More menu of a repository.")
    await reply(update, context, join_limited(lines), kb(rows) if rows else None)


async def cmd_favorites(update: Update, context: Ctx) -> None:
    await show_favorites(update, context)


@callback("favorites")
async def cb_favorites(update: Update, context: Ctx, data: tuple) -> None:
    await show_favorites(update, context)


@callback("fav_rm")
async def cb_fav_rm(update: Update, context: Ctx, data: tuple) -> None:
    await svc(context).db.remove_favorite(data[1])
    await show_favorites(update, context)

