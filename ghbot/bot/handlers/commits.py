"""Branches, commits, comparisons, contributors, history statistics and read-only history analysis."""

from __future__ import annotations

import asyncio
import secrets
import shutil

from telegram import Update

from ghbot.bot.handlers.operations import start_operation
from ghbot.bot.handlers.repos import readable, repo_ref, show_repo_list
from ghbot.bot.router import callback
from ghbot.bot.ui import Ctx, ProgressMessage, ago, btn, code, h, join_limited, kb, pager, reply, safe_error, svc
from ghbot.git.stats import blobless_clone, history_stats
from ghbot.github.client import GitHubError, GitHubNotFound
from ghbot.services.operations import DEFAULT_SQUASH_STYLE
from ghbot.validators import RepoRef, ValidationError, cutoff_datetime, parse_date, validate_branch_name, validate_ref


def _back(repo: RepoRef) -> list:
    return [btn("⬅️ Repository", "pick", "detail", repo.full_name)]


async def show_commits(update: Update, context: Ctx, repo: RepoRef, branch: str | None, page: int,
                       since: str | None = None, until: str | None = None) -> None:
    repo, _ = await readable(context, repo.full_name)
    s = svc(context)
    if branch:
        branch = validate_branch_name(branch)
    try:
        result = await s.gh.list_commits(repo.full_name, page=page, per_page=10, sha=branch, since=since, until=until)
    except GitHubError as exc:
        if exc.status == 409:
            await reply(update, context, f"{code(repo)} is empty (no commits).")
            return
        raise
    scope = f" · branch {code(branch)}" if branch else " · default branch"
    if since:
        scope += f" · after {h(since[:10])}"
    if until:
        scope += f" · before {h(until[:10])}"
    lines = [f"🔀 <b>Commits</b> · {code(repo)}{scope}", ""]
    rows = []
    for item in result.items:
        c = item["commit"]
        author = (item.get("author") or {}).get("login") or c["author"]["name"]
        lines.append(f"{code(item['sha'][:7])} {ago(c['author']['date'])} <i>{h(author)}</i>\n    "
                     f"{h(c['message'].splitlines()[0][:90])}")
    if not result.items:
        lines.append("No commits.")
    if since or until:
        rows.append(pager("commits_range", page, result.has_next, repo.full_name, branch or "", since or "", until or ""))
        lines.append("\n<i>Read-only listing. Nothing is changed.</i>")
    else:
        rows.append(pager("commits", page, result.has_next, repo.full_name, branch or ""))
    rows.append([btn("🌿 Branches", "branches", repo.full_name, 1), btn("🔬 Analyze history", "pick", "analyze", repo.full_name)])
    rows.append(_back(repo))
    await reply(update, context, join_limited(lines), kb(rows))


@callback("commits")
async def cb_commits(update: Update, context: Ctx, data: tuple) -> None:
    await show_commits(update, context, repo_ref(context, data[1]), data[2] or None, int(data[3]))


@callback("commits_range")
async def cb_commits_range(update: Update, context: Ctx, data: tuple) -> None:
    await show_commits(update, context, repo_ref(context, data[1]), data[2] or None, int(data[5]),
                       since=data[3] or None, until=data[4] or None)


async def show_branches(update: Update, context: Ctx, repo: RepoRef, page: int) -> None:
    repo, meta = await readable(context, repo.full_name)
    result = await svc(context).gh.list_branches(repo.full_name, page=page, per_page=15)
    lines = [f"🌿 <b>Branches</b> · {code(repo)}", f"Default branch: ⭐ {code(meta.get('default_branch'))}", ""]
    rows = []
    for b in result.items:
        marks = ("⭐ " if b["name"] == meta.get("default_branch") else "") + ("🛡 " if b.get("protected") else "")
        lines.append(f"{marks}{code(b['name'])} → {code(b['commit']['sha'][:7])}")
        rows.append([btn(f"{marks}{b['name']}"[:60], "commits", repo.full_name, b["name"], 1)])
    lines.append("\n⭐ default · 🛡 protected")
    rows.append(pager("branches", page, result.has_next, repo.full_name, last_page=result.last_page))
    rows.append(_back(repo))
    await reply(update, context, join_limited(lines), kb(rows))


@callback("branches")
async def cb_branches(update: Update, context: Ctx, data: tuple) -> None:
    await show_branches(update, context, repo_ref(context, data[1]), int(data[2]))


async def cmd_branches(update: Update, context: Ctx) -> None:
    if not context.args:
        raise ValidationError("Usage: /branches repo")
    await show_branches(update, context, repo_ref(context, context.args[0]), 1)


async def cmd_commits(update: Update, context: Ctx) -> None:
    args = context.args or []
    if not args:
        await show_repo_list(update, context, "commits", 1)
        return
    await show_commits(update, context, repo_ref(context, args[0]), args[1] if len(args) > 1 else None, 1)


async def cmd_latest(update: Update, context: Ctx) -> None:
    if not context.args:
        await show_repo_list(update, context, "commits", 1)
        return
    await show_commits(update, context, repo_ref(context, context.args[0]), None, 1)


def _date_args(context: Ctx, usage: str) -> tuple[RepoRef, str, str | None]:
    args = context.args or []
    if len(args) not in (2, 3):
        raise ValidationError(f"Usage: {usage}")
    moment = cutoff_datetime(parse_date(args[1])).strftime("%Y-%m-%dT%H:%M:%SZ")
    return repo_ref(context, args[0]), moment, args[2] if len(args) == 3 else None


async def cmd_commits_before(update: Update, context: Ctx) -> None:
    repo, moment, branch = _date_args(context, "/commits_before repo YYYY-MM-DD [branch]")
    await show_commits(update, context, repo, branch, 1, until=moment)


async def cmd_commits_after(update: Update, context: Ctx) -> None:
    repo, moment, branch = _date_args(context, "/commits_after repo YYYY-MM-DD [branch]")
    await show_commits(update, context, repo, branch, 1, since=moment)


async def cmd_commit(update: Update, context: Ctx) -> None:
    args = context.args or []
    if len(args) != 2:
        raise ValidationError("Usage: /commit repo sha")
    repo, _ = await readable(context, args[0])
    ref = validate_ref(args[1])
    try:
        c = await svc(context).gh.get_commit(repo.full_name, ref)
    except (GitHubNotFound, GitHubError) as exc:
        if getattr(exc, "status", 0) in (404, 422):
            raise ValidationError(f"Commit {ref} was not found in {repo.full_name}.") from None
        raise
    info = c["commit"]
    stats = c.get("stats") or {}
    verification = (info.get("verification") or {})
    lines = [
        f"🔎 <b>Commit {h(c['sha'][:12])}</b> · {code(repo)}", "",
        f"<b>Author:</b> {h(info['author']['name'])} &lt;{h(info['author']['email'])}&gt; · {ago(info['author']['date'])}",
        f"<b>Committer:</b> {h(info['committer']['name'])} · {ago(info['committer']['date'])}",
        f"<b>SHA:</b> {code(c['sha'])}",
        f"<b>Parents:</b> {' '.join(code(p['sha'][:7]) for p in c.get('parents', [])) or 'none (root commit)'}",
        f"<b>Signature:</b> {'✅ verified' if verification.get('verified') else h(verification.get('reason') or 'unsigned')}",
        f"<b>Changes:</b> {len(c.get('files', []))} file(s), +{stats.get('additions', 0)} −{stats.get('deletions', 0)}",
        "", "<b>Message</b>", f"<pre>{h(info['message'][:1500])}</pre>", "<b>Files</b>",
    ]
    for f in c.get("files", [])[:25]:
        lines.append(f"{h(f['status'][:1].upper())} {code(f['filename'])} +{f.get('additions', 0)} −{f.get('deletions', 0)}")
    if len(c.get("files", [])) > 25:
        lines.append(f"… {len(c['files']) - 25} more")
    await reply(update, context, join_limited(lines), kb([_back(repo)]))


async def cmd_compare(update: Update, context: Ctx) -> None:
    args = context.args or []
    if len(args) != 3:
        raise ValidationError("Usage: /compare repo base head (branches, tags or SHAs)")
    repo, _ = await readable(context, args[0])
    base, head = validate_ref(args[1]), validate_ref(args[2])
    try:
        result = await svc(context).gh.compare(repo.full_name, base, head)
    except GitHubError as exc:
        if exc.status in (404, 422):
            raise ValidationError("One of the refs was not found, or they share no history.") from None
        raise
    status = {"identical": "🟰 identical", "ahead": "⬆️ head is ahead", "behind": "⬇️ head is behind",
              "diverged": "↔️ diverged"}.get(result["status"], result["status"])
    lines = [
        f"↔️ <b>Compare</b> {code(base)} … {code(head)} · {code(repo)}", "",
        f"Status: {h(status)}",
        f"Ahead by: {result['ahead_by']} · Behind by: {result['behind_by']}",
        f"Commits: {result['total_commits']} · Files changed: {len(result.get('files') or [])}",
        f"Merge base: {code((result.get('merge_base_commit') or {}).get('sha', '')[:7])}", "",
    ]
    for c in (result.get("commits") or [])[-10:]:
        lines.append(f"{code(c['sha'][:7])} {h(c['commit']['message'].splitlines()[0][:80])}")
    await reply(update, context, join_limited(lines), kb([_back(repo)]))


async def show_contributors(update: Update, context: Ctx, text: str, page: int) -> None:
    repo, _ = await readable(context, text)
    result = await svc(context).gh.list_contributors(repo.full_name, page=page)
    lines = [f"👥 <b>Contributors · {h(repo.full_name)}</b>", ""]
    start = (page - 1) * 15
    for i, person in enumerate(result.items, start + 1):
        lines.append(f"{i}. <b>{h(person.get('login') or person.get('name') or 'anonymous')}</b> — {person['contributions']} commit(s)")
    if not result.items:
        lines.append("No contributors (empty repository).")
    await reply(update, context, join_limited(lines),
                kb([pager("contributors", page, result.has_next, repo.full_name, last_page=result.last_page), _back(repo)]))


async def cmd_contributors(update: Update, context: Ctx) -> None:
    if not context.args:
        raise ValidationError("Usage: /contributors repo")
    await show_contributors(update, context, context.args[0], 1)


@callback("contributors")
async def cb_contributors(update: Update, context: Ctx, data: tuple) -> None:
    await show_contributors(update, context, data[1], int(data[2]))


async def show_history_stats(update: Update, context: Ctx, text: str) -> None:
    repo, _ = await readable(context, text)
    s = svc(context)
    progress = await ProgressMessage.create(update, context, f"📊 Reading history of {code(repo)} (read-only, no file contents)…")
    work = s.settings.work_path / f"stats-{secrets.token_hex(4)}"
    try:
        work.mkdir(parents=True)
        clone = work / "repo.git"
        await asyncio.to_thread(blobless_clone, s.git, s.backups.url_for(repo.full_name), clone)
        stats = await asyncio.to_thread(history_stats, s.git, clone)
    except Exception as exc:  # noqa: BLE001
        await progress.finish(context, update, f"❌ Could not read history: {safe_error(exc)}")
        return
    finally:
        await asyncio.to_thread(shutil.rmtree, work, True)
    lines = [
        f"📊 <b>History statistics · {h(repo.full_name)}</b>", "",
        f"Commits (all branches &amp; tags): {stats.total_commits}",
        f"Date range: {h(stats.first_commit or '—')} → {h(stats.last_commit or '—')}",
        f"Branches: {stats.branches} · Tags: {stats.tags}",
        f"Merge commits: {stats.merges} · Root commits: {stats.roots}",
        f"Authors: {stats.authors} · Busiest weekday: {h(stats.busiest_weekday or '—')}",
        "", "<b>Commits per year</b>",
    ]
    peak = max(stats.per_year.values(), default=1)
    lines += [f"{code(year)} {'█' * max(1, round(n * 20 / peak))} {n}" for year, n in stats.per_year.items()] or ["—"]
    lines += ["", "<b>Top authors</b>"] + [f"• {h(name)} — {n}" for name, n in stats.top_authors]
    await progress.finish(context, update, join_limited(lines), kb([_back(repo)]))


async def cmd_history_stats(update: Update, context: Ctx) -> None:
    if not context.args:
        raise ValidationError("Usage: /history_stats repo")
    await show_history_stats(update, context, context.args[0])


@callback("hist_stats")
async def cb_hist_stats(update: Update, context: Ctx, data: tuple) -> None:
    await show_history_stats(update, context, data[1])


async def cmd_analyze(update: Update, context: Ctx) -> None:
    args = context.args or []
    if len(args) != 2:
        if not args:
            await show_repo_list(update, context, "analyze", 1)
            return
        raise ValidationError("Usage: /analyze repo-name YYYY-MM-DD")
    repo = repo_ref(context, args[0])
    cutoff = parse_date(args[1])
    await start_operation(update, context, "rewrite_history", repo, {"cutoff": cutoff.isoformat(), "tag_policy": "snapshot", "squash_style": DEFAULT_SQUASH_STYLE})
