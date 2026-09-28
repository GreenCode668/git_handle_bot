"""Repository listing, details, simple actions, `repo @action` syntax and import."""

from __future__ import annotations

from telegram import Update

from ghbot.bot.handlers.operations import start_operation
from ghbot.bot.router import await_input, callback, clear_input, text_input
from ghbot.bot.ui import (
    Ctx,
    ProgressMessage,
    ago,
    btn,
    code,
    h,
    join_limited,
    kb,
    pager,
    reply,
    run_emoji,
    safe_error,
    svc,
    url_btn,
)
from ghbot.github.client import GitHubError
from ghbot.services.operations import DEFAULT_SQUASH_STYLE, ImportRepository
from ghbot.validators import (
    RepoRef,
    ValidationError,
    parse_date,
    parse_repo,
    parse_repo_action,
    suggest_repo_name,
    validate_git_url,
    validate_repo_name,
)

PURPOSES = {
    "detail": "📁 Repositories",
    "commits": "🔀 Choose a repository for commits",
    "actions": "⚙️ Choose a repository for GitHub Actions",
    "backup": "💾 Choose a repository to back up",
    "analyze": "🔬 Choose a repository to analyze",
    "pr": "🏆 Choose a repository for a pull request",
}


def repo_ref(context: Ctx, text: str) -> RepoRef:
    return parse_repo(text, svc(context).username)


async def readable(context: Ctx, text: str) -> tuple[RepoRef, dict]:
    """Resolve a repository for a read-only view; hidden/private repositories are refused."""
    repo = repo_ref(context, text)
    meta = await svc(context).policy.readable(repo)
    return RepoRef(repo.owner, meta["name"]), meta


def repo_flags(meta: dict, favorites: set[str] | None = None) -> str:
    return (("⭐" if favorites and meta["full_name"].lower() in favorites else "")
            + ("🔒" if meta["private"] else "🌍") + ("📦" if meta["archived"] else "") + ("🍴" if meta.get("fork") else ""))


async def page_size(context: Ctx) -> int:
    return int(await svc(context).db.get_setting("page_size", 8))


async def show_repo_list(update: Update, context: Ctx, purpose: str = "detail", page: int = 1, sort: str = "pushed") -> None:
    s = svc(context)
    result = await s.gh.list_repos(page, await page_size(context), sort=sort, public_only=not s.policy.show_private)
    items = s.policy.filter_visible(result.items)
    favorites = await s.db.list_favorites()
    fav_keys = {f.lower() for f in favorites}
    title = PURPOSES.get(purpose, PURPOSES["detail"]) + (" · recently updated" if sort == "updated" else "")
    lines = [f"<b>{title}</b>", ""]
    rows = []
    if page == 1 and favorites:
        lines.append("⭐ Favorites: " + ", ".join(h(f.split("/", 1)[1]) for f in favorites))
        rows += [[btn(f"⭐ {f.split('/', 1)[1]}", "pick", purpose, f) for f in favorites[i:i + 2]]
                 for i in range(0, min(len(favorites), 6), 2)]
        lines.append("")
    for repo in items:
        flags = repo_flags(repo, fav_keys)
        when = repo.get("updated_at") if sort == "updated" else repo.get("pushed_at")
        lines.append(f"{flags} <b>{h(repo['name'])}</b> · {'updated' if sort == 'updated' else 'pushed'} {ago(when)}")
        rows.append([btn(f"{flags} {repo['name']}", "pick", purpose, repo["full_name"])])
    if not items:
        lines.append("No repositories found.")
    if sort == "updated":
        rows.append(pager("recent", page, result.has_next, last_page=result.last_page))
    else:
        rows.append(pager("repos", page, result.has_next, purpose, last_page=result.last_page))
    if purpose == "detail":
        rows.append([btn("🔎 Search", "search_help"), btn("🕒 Recent", "recent", 1), btn("⭐ Favorites", "favorites")])
        rows.append([btn("➕ Create repository", "create_help"), btn("📥 Import repository", "import_help")])
    if purpose in ("detail", "commits", "analyze"):
        rows.append([btn("🔬 Analyze old commits in many repositories", "mb_start")])
    if purpose == "detail":
        rows.append([btn("📖 Guide", "guide", "repos")])
    if not s.policy.show_private:
        lines.append("\n<i>Only public repositories are shown.</i>")
    await reply(update, context, join_limited(lines), kb(rows))


@callback("repos")
async def cb_repos(update: Update, context: Ctx, data: tuple) -> None:
    await show_repo_list(update, context, data[1], int(data[2]))


@callback("recent")
async def cb_recent(update: Update, context: Ctx, data: tuple) -> None:
    await show_repo_list(update, context, "detail", int(data[1]), sort="updated")


@callback("pick")
async def cb_pick(update: Update, context: Ctx, data: tuple) -> None:
    purpose, full_name = data[1], data[2]
    repo = repo_ref(context, full_name)
    if purpose == "commits":
        from ghbot.bot.handlers.commits import show_commits

        await show_commits(update, context, repo, None, 1)
    elif purpose == "actions":
        from ghbot.bot.handlers.actions import show_actions

        await show_actions(update, context, repo, "all", 1)
    elif purpose == "backup":
        from ghbot.bot.handlers.backups import create_backup

        await create_backup(update, context, repo)
    elif purpose == "pr":
        from ghbot.bot.handlers.pullrequests import start_change

        await start_change(update, context, repo.full_name)
    elif purpose == "analyze":
        await_input(context, "analyze_date", repo=repo.full_name)
        await reply(update, context, f"🔬 Send only the cutoff date for {code(repo)}, e.g. <code>2013-01-01</code>.\n"
                                     "Commits dated before it are analyzed. Analysis never modifies anything. /cancel to abort.")
    else:
        await show_repo_detail(update, context, repo)


async def show_repo_detail(update: Update, context: Ctx, repo: RepoRef) -> None:
    s = svc(context)
    meta = await s.policy.readable(repo)

    async def optional(coro):
        try:
            return await coro
        except GitHubError:  # empty repositories answer 409, disabled Actions 404
            return None

    branches = await optional(s.gh.list_branches(repo.full_name, per_page=1))
    branch_count = (branches.last_page or len(branches.items)) if branches else "?"
    commits = await optional(s.gh.list_commits(repo.full_name, per_page=1))
    last = commits.items[0] if commits and commits.items else None
    runs = await optional(s.gh.list_runs(repo.full_name, per_page=1))
    backups, backup_total = await s.db.list_backups(limit=1, repo=repo.full_name)

    lines = [
        f"📁 <b>{h(meta['full_name'])}</b>",
        h(meta.get("description") or ""),
        "",
        f"Visibility: {'🔒 private' if meta['private'] else '🌍 public'}{' · 📦 archived' if meta['archived'] else ''}"
        f"{' · 🍴 fork' if meta['fork'] else ''}",
        f"Default branch: {code(meta.get('default_branch'))} · Branches: {branch_count}",
        f"⭐ {meta['stargazers_count']} · 🍴 {meta['forks_count']} · Issues/PRs open: {meta['open_issues_count']}",
        f"Language: {h(meta.get('language') or '—')} · Size: {meta.get('size', 0)} KB",
        f"Created {ago(meta.get('created_at'))} · Pushed {ago(meta.get('pushed_at'))}",
    ]
    if last:
        lines.append(f"Last commit: {code(last['sha'][:7])} {h(last['commit']['message'].splitlines()[0][:80])}")
    if runs and runs.items:
        run = runs.items[0]
        lines.append(f"Actions: {run_emoji(run)} {h(run.get('name'))} ({h(run.get('head_branch'))})")
    lines.append(f"Backups: {backup_total}" + (f" · latest {code(backups[0].id)} ({backups[0].status})" if backups else ""))
    if meta["private"]:
        lines.append("\n🔒 <b>Read-only:</b> private repositories are never modified by this bot.")
    if await s.db.get_protection(meta["full_name"]):
        lines.append("🛡 Manually protected: write/destructive operations are blocked (/unlock).")
    lines.append(f"\nShortcuts: <code>{h(meta['name'])} @stats</code>, @commits, @branches, @actions, @backup, "
                 "@rename new-name, @archive, @unarchive, @remove")

    name = meta["full_name"]
    rows = [
        [btn("🌿 Branches", "branches", name, 1), btn("🔀 Commits", "commits", name, "", 1)],
        [btn("⚙️ Actions", "actions", name, "all", 1), btn("💾 Backup now", "pick", "backup", name)],
        [btn("✏️ Rename", "repo_rename", name),
         btn("📤 Unarchive", "repo_act", name, "unarchive") if meta["archived"] else btn("📦 Archive", "repo_act", name, "archive")],
        [btn("🔬 Analyze history", "pick", "analyze", name), btn("➕ More", "repo_more", name)],
        [btn("🗑 Delete repository", "repo_act", name, "remove")],
        [btn("🔄 Refresh", "pick", "detail", name), url_btn("🔗 Open", meta["html_url"]) if meta.get("html_url") else None,
         btn("⬅️ Repositories", "repos", "detail", 1)],
    ]
    rows[-1] = [b for b in rows[-1] if b]
    await reply(update, context, join_limited(lines), kb(rows))


ACTION_KINDS = {
    "remove": "delete_repo", "private": "make_private", "public": "make_public",
    "archive": "archive_repo", "unarchive": "unarchive_repo",
}


async def run_repo_action(update: Update, context: Ctx, repo_text: str, action: str, arg: str | None) -> None:
    repo = repo_ref(context, repo_text)
    if action == "info":
        await show_repo_detail(update, context, repo)
    elif action == "commits":
        from ghbot.bot.handlers.commits import show_commits

        await show_commits(update, context, repo, None, 1)
    elif action == "branches":
        from ghbot.bot.handlers.commits import show_branches

        await show_branches(update, context, repo, 1)
    elif action == "actions":
        from ghbot.bot.handlers.actions import show_actions

        await show_actions(update, context, repo, "all", 1)
    elif action == "backup":
        from ghbot.bot.handlers.backups import create_backup

        await create_backup(update, context, repo)
    elif action == "stats":
        from ghbot.bot.handlers.repoinfo import show_stats

        await show_stats(update, context, repo)
    elif action == "rename":
        if not arg:
            await_input(context, "rename", repo=repo.full_name)
            await reply(update, context, f"✏️ Send the new name for {code(repo)}. /cancel to abort.")
            return
        await start_operation(update, context, "rename_repo", repo, {"new_name": validate_repo_name(arg)})
    elif action in ACTION_KINDS:
        await start_operation(update, context, ACTION_KINDS[action], repo, {})
    else:
        raise ValidationError("Unknown action.")


@callback("repo_act")
async def cb_repo_act(update: Update, context: Ctx, data: tuple) -> None:
    await run_repo_action(update, context, data[1], data[2], None)


@callback("repo_rename")
async def cb_rename(update: Update, context: Ctx, data: tuple) -> None:
    await run_repo_action(update, context, data[1], "rename", None)


@text_input("rename")
async def input_rename(update: Update, context: Ctx, state: dict, text: str) -> None:
    clear_input(context)
    await start_operation(update, context, "rename_repo", repo_ref(context, state["repo"]),
                          {"new_name": validate_repo_name(text)})


@text_input("analyze_date")
async def input_analyze_date(update: Update, context: Ctx, state: dict, text: str) -> None:
    parts = text.split()
    if len(parts) == 2:  # "repo date" is accepted too, but only for the repository being analyzed
        if repo_ref(context, parts[0]).key != state["repo"].lower():
            raise ValidationError(f"You are analyzing {state['repo']}. Send only the date, e.g. 2013-01-01.")
        parts = parts[1:]
    if len(parts) != 1:
        raise ValidationError(f"Send only the cutoff date for {state['repo']}, e.g. 2013-01-01.")
    cutoff = parse_date(parts[0])
    clear_input(context)
    await start_operation(update, context, "rewrite_history", repo_ref(context, state["repo"]),
                          {"cutoff": cutoff.isoformat(), "tag_policy": "snapshot", "squash_style": DEFAULT_SQUASH_STYLE})


async def try_repo_action_text(update: Update, context: Ctx, text: str) -> bool:
    action = parse_repo_action(text)
    if action is None:
        return False
    await run_repo_action(update, context, action.repo, action.action, action.arg)
    return True


async def cmd_repos(update: Update, context: Ctx) -> None:
    page = int(context.args[0]) if context.args and context.args[0].isdigit() else 1
    await show_repo_list(update, context, "detail", max(1, page))


async def cmd_repo(update: Update, context: Ctx) -> None:
    args = context.args or []
    if not args:
        await reply(update, context, "Usage: <code>/repo name</code> or <code>/repo name @action [arg]</code>\n"
                                     "Actions: @info @rename new-name @private @public @archive @unarchive @remove")
        return
    if len(args) == 1:
        await show_repo_detail(update, context, repo_ref(context, args[0]))
        return
    action = parse_repo_action(" ".join(args))
    if action is None:
        raise ValidationError("Could not parse that action. Example: /repo my-repo @rename new-name")
    await run_repo_action(update, context, action.repo, action.action, action.arg)


# ------------------------------------------------------------------- import
IMPORT_HELP = ("📥 <b>Import a repository</b>\nSend <code>/import https://host/owner/repo.git</code>.\n"
               "Only credential-free https URLs are accepted. The source is analyzed first.")


@callback("import_help")
async def cb_import_help(update: Update, context: Ctx, data: tuple) -> None:
    await_input(context, "import_url")
    await reply(update, context, IMPORT_HELP + "\n\nOr just send the URL now. /cancel to abort.")


@text_input("import_url")
async def input_import_url(update: Update, context: Ctx, state: dict, text: str) -> None:
    clear_input(context)
    await analyze_import(update, context, text)


async def cmd_import(update: Update, context: Ctx) -> None:
    if not context.args:
        await_input(context, "import_url")
        await reply(update, context, IMPORT_HELP + "\n\nSend the URL now, or /cancel.")
        return
    await analyze_import(update, context, context.args[0])


async def analyze_import(update: Update, context: Ctx, raw_url: str) -> None:
    url = validate_git_url(raw_url)
    s = svc(context)
    progress = await ProgressMessage.create(update, context, "🔍 Inspecting source repository (read-only)…")
    spec = s.safety.spec("import_repo")
    assert isinstance(spec, ImportRepository)
    try:
        info = await spec.inspect_source(url)
    except Exception as exc:  # noqa: BLE001
        await progress.finish(context, update, f"❌ Could not read the source repository: {safe_error(exc)}")
        return
    suggested = suggest_repo_name(url)
    context.user_data["import"] = {"url": url, "name": suggested}  # type: ignore[index]
    await_input(context, "import_name")
    lines = [
        "📥 <b>Source analysis</b>", f"URL: {code(url)}",
        f"Branches: {info['branches']} · Tags: {info['tags']} · Other refs (not imported): {info['other']}",
        f"Default branch: {code(info['head'] or 'unknown')}",
        "History, branches and tags will be preserved.",
        "⚠️ The destination will be a <b>PUBLIC</b> repository (public-only policy).", "",
        f"Send a destination name, or use the suggested name {code(suggested)}.",
    ]
    await progress.finish(context, update, "\n".join(lines), kb([[btn(f"✅ Use {suggested}", "import_name", suggested)],
                                                                 [btn("✖️ Cancel", "import_cancel")]]))


@text_input("import_name")
async def input_import_name(update: Update, context: Ctx, state: dict, text: str) -> None:
    await choose_import_name(update, context, text)


@callback("import_name")
async def cb_import_name(update: Update, context: Ctx, data: tuple) -> None:
    await choose_import_name(update, context, data[1])


async def choose_import_name(update: Update, context: Ctx, name: str) -> None:
    pending = context.user_data.get("import")  # type: ignore[union-attr]
    if not pending:
        await reply(update, context, "No import in progress. Use /import.")
        return
    pending["name"] = validate_repo_name(name)
    clear_input(context)
    context.user_data.pop("import", None)  # type: ignore[union-attr]
    # Public-only policy: imports are always created as public repositories.
    await start_operation(update, context, "import_repo", repo_ref(context, pending["name"]),
                          {"url": pending["url"], "private": False})


@callback("import_cancel")
async def cb_import_cancel(update: Update, context: Ctx, data: tuple) -> None:
    context.user_data.pop("import", None)  # type: ignore[union-attr]
    clear_input(context)
    await reply(update, context, "✖️ Import cancelled. Nothing was changed.")


__all__ = ["cmd_import", "cmd_repo", "cmd_repos", "show_repo_list", "try_repo_action_text", "GitHubError"]
