"""/aboutme: create and manage the profile README shown on your GitHub profile."""

from __future__ import annotations

import secrets

from telegram import Update

from ghbot.bot.handlers.operations import start_operation
from ghbot.bot.handlers.repos import repo_ref
from ghbot.bot.router import await_input, callback, clear_input, text_input
from ghbot.bot.ui import Ctx, ProgressMessage, btn, code, fmt_bytes, h, join_limited, kb, reply, safe_error, svc
from ghbot.github.client import GitHubError
from ghbot.services.badges import Badge
from ghbot.services.profile_readme import publish_readme, read_state, render_template, validate_readme
from ghbot.validators import ValidationError


def profile_repo(context: Ctx) -> str:
    user = svc(context).username
    return f"{user}/{user}"


async def show_aboutme(update: Update, context: Ctx) -> None:
    s = svc(context)
    full = profile_repo(context)
    state = await read_state(s, full)
    lines = [f"🪪 <b>Profile README</b> · {code(full)}", ""]
    rows = []
    if not state.exists:
        lines += [
            "❌ The repository does not exist yet.",
            f"GitHub shows a profile README only from a <b>public repository named exactly "
            f"{h(s.username)}</b>.",
            "Create it first, then choose what it should contain.",
        ]
        rows.append([btn("➕ Create the profile repository", "ab_create")])
    else:
        if state.has_readme:
            lines.append(f"✅ README.md exists · {fmt_bytes(len(state.readme.encode()))}")
            preview = state.readme.strip().splitlines()[:6]
            lines += ["", "<b>Starts with</b>", f"<pre>{h(chr(10).join(preview))[:600]}</pre>"]
        else:
            lines.append("⚠️ The repository exists but has no README.md yet.")
        lines += ["", "Choose what to publish. You always see a preview and confirm before anything is written."]
        rows += [
            [btn("📋 Copy from another repository", "ab_copy", 1)],
            [btn("🎨 Build from my profile", "ab_template")],
            [btn("✍️ Send my own text", "ab_text")],
            [btn("↩️ Revert last change", "ab_revert"), btn("🏅 Badges", "badges")],
        ]
    rows.append([btn("🔄 Refresh", "aboutme"), btn("📖 Guide", "guide", "profile")])
    await reply(update, context, join_limited(lines), kb(rows))


async def cmd_aboutme(update: Update, context: Ctx) -> None:
    await show_aboutme(update, context)


@callback("aboutme")
async def cb_aboutme(update: Update, context: Ctx, data: tuple) -> None:
    await show_aboutme(update, context)


@callback("ab_create")
async def cb_create(update: Update, context: Ctx, data: tuple) -> None:
    username = svc(context).username
    await start_operation(update, context, "create_repo", repo_ref(context, username),
                          {"description": f"Profile README of {username}", "private": False})


# ------------------------------------------------------------------ sources
@callback("ab_copy")
async def cb_copy(update: Update, context: Ctx, data: tuple) -> None:
    s = svc(context)
    page = int(data[1])
    result = await s.gh.list_repos(page, 8, public_only=not s.policy.show_private)
    items = [r for r in s.policy.filter_visible(result.items) if r["full_name"] != profile_repo(context)]
    rows = [[btn(r["name"], "ab_copy_pick", r["full_name"])] for r in items]
    nav = []
    if page > 1:
        nav.append(btn("◀️ Prev", "ab_copy", page - 1))
    if result.has_next:
        nav.append(btn("Next ▶️", "ab_copy", page + 1))
    rows.append(nav or [btn("⬅️ Back", "aboutme")])
    if nav:
        rows.append([btn("⬅️ Back", "aboutme")])
    await reply(update, context, "📋 Which repository's README.md should be copied?", kb(rows))


@callback("ab_copy_pick")
async def cb_copy_pick(update: Update, context: Ctx, data: tuple) -> None:
    s = svc(context)
    source = await s.gh.get_file(data[1], "README.md")
    if source is None:
        await reply(update, context, f"{code(data[1])} has no README.md.", kb([[btn("⬅️ Back", "ab_copy", 1)]]))
        return
    await propose(update, context, source[0], f"copied from {data[1]}")


@callback("ab_template")
async def cb_template(update: Update, context: Ctx, data: tuple) -> None:
    s = svc(context)
    progress = await ProgressMessage.create(update, context, "🎨 Building from your profile…")
    user = await s.gh.get_user()
    try:
        socials = [a["url"] for a in await s.gh.list_social_accounts()]
    except GitHubError:
        socials = []
    badges = [Badge(b["id"], b["kind"], b["label"], b["config"]) for b in await s.db.list_badges()]
    content = render_template(s.username, user, badges, socials)
    await progress.finish(context, update, "🎨 Template ready.")
    await propose(update, context, content, "built from your profile", edit=False)


@callback("ab_text")
async def cb_text(update: Update, context: Ctx, data: tuple) -> None:
    await_input(context, "aboutme_text")
    await reply(update, context, "✍️ Send the README content as one message (Markdown and HTML both work).\n"
                                 "Telegram limits a message to about 4000 characters; for longer pages use "
                                 "<b>📋 Copy from another repository</b>. /cancel to abort.")


@text_input("aboutme_text")
async def input_text(update: Update, context: Ctx, state: dict, text: str) -> None:
    clear_input(context)
    await propose(update, context, text, "your message", edit=False)


# ------------------------------------------------------------------ publish
async def propose(update: Update, context: Ctx, content: str, source: str, edit: bool = True) -> None:
    content = validate_readme(content)
    state = await read_state(svc(context), profile_repo(context))
    if not state.exists:
        await reply(update, context, "Create the profile repository first.", kb([[btn("➕ Create", "ab_create")]]))
        return
    if state.readme == content:
        await reply(update, context, "ℹ️ That is already the current README.", kb([[btn("⬅️ Back", "aboutme")]]))
        return
    nonce = secrets.token_hex(4)
    context.user_data["aboutme"] = {"nonce": nonce, "content": content, "source": source}  # type: ignore[index]
    preview = "\n".join(content.strip().splitlines()[:12])
    lines = [
        f"🪪 <b>Publish profile README</b> ({h(source)})",
        f"Target: {code(profile_repo(context) + '/README.md')}",
        f"Size: {fmt_bytes(len(content.encode()))}"
        + (f" · replaces {fmt_bytes(len(state.readme.encode()))}" if state.has_readme else " · new file"),
        "", "<b>Preview (first lines)</b>", f"<pre>{h(preview)[:1500]}</pre>",
        "", "The previous README is saved first, so ↩️ Revert can undo this.",
    ]
    await reply(update, context, join_limited(lines),
                kb([[btn("✅ Publish", "ab_publish", nonce), btn("✖️ Cancel", "aboutme")]]), edit=edit)


@callback("ab_publish")
async def cb_publish(update: Update, context: Ctx, data: tuple) -> None:
    pending = context.user_data.pop("aboutme", None)  # type: ignore[union-attr]
    if not pending or pending["nonce"] != data[1]:
        await reply(update, context, "That confirmation expired. Start again with /aboutme.")
        return
    s = svc(context)
    progress = await ProgressMessage.create(update, context, "🚀 Publishing…")
    try:
        ok, _ = await publish_readme(s, profile_repo(context), pending["content"], "Update profile README via Telegram bot")
    except (GitHubError, ValidationError) as exc:
        hint = " The README changed on GitHub meanwhile; nothing was overwritten." if getattr(exc, "status", 0) in (409, 422) else ""
        await progress.finish(context, update, f"❌ {safe_error(exc)}{hint}")
        return
    await progress.finish(
        context, update,
        ("✅ Published and verified. Open your GitHub profile to see it." if ok
         else "⚠️ Published, but the content read back differs. Check the repository."),
        kb([[btn("🪪 Profile README", "aboutme")]]),
    )


@callback("ab_revert")
async def cb_revert(update: Update, context: Ctx, data: tuple) -> None:
    s = svc(context)
    full = profile_repo(context)
    snapshot = await s.db.latest_readme_snapshot(full)
    if snapshot is None:
        await reply(update, context, "No previous version saved yet.", kb([[btn("⬅️ Back", "aboutme")]]))
        return
    await propose(update, context, snapshot["content"], f"snapshot from {snapshot['created_at']}")
