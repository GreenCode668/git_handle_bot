"""/reauthor: map commit author identities in one repository to your own."""

from __future__ import annotations

import asyncio
import re
import secrets
import shutil

from telegram import Update

from ghbot.bot.handlers.operations import start_operation
from ghbot.bot.handlers.repos import readable, repo_ref
from ghbot.bot.router import await_input, callback, clear_input, text_input
from ghbot.bot.ui import Ctx, ProgressMessage, btn, code, h, join_limited, kb, reply, safe_error, svc
from ghbot.git.history import author_stats
from ghbot.validators import ValidationError

IDENTITY_RE = re.compile(r"^(?P<name>[^<>\n]{1,80})?\s*<?(?P<email>[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+)>?$")


async def target_identity(context: Ctx) -> tuple[str, str | None]:
    """The commit identity of the active account.

    Order: what you set in the bot for this account → GITHUB_EMAIL/GITHUB_NAME in .env for it →
    the account's username. Renaming an account therefore never leaves an old username behind.
    """
    s = svc(context)
    account = s.account
    email = (await s.db.get_setting(s.setting_key("author_email"))
             or account.email
             or await s.db.get_setting("author_email"))  # pre-multi-account setting
    if await s.db.get_setting(s.setting_key("author_name_custom"), False):
        return await s.db.get_setting(s.setting_key("author_name"), s.username), email
    return account.display_name or s.username, email


async def identity_source(context: Ctx) -> str:
    s = svc(context)
    if await s.db.get_setting(s.setting_key("author_email")):
        return "set in the bot for this account"
    if s.account.email:
        return "from .env"
    if await s.db.get_setting("author_email"):
        return "set in the bot before accounts existed"
    return "not set yet"


async def show_authors(update: Update, context: Ctx, repo_text: str) -> None:
    s = svc(context)
    repo, _ = await readable(context, repo_text)
    progress = await ProgressMessage.create(update, context, f"📥 Reading authors of {code(repo)} (read-only)…")
    work = s.settings.work_path / f"authors-{secrets.token_hex(4)}"
    try:
        work.mkdir(parents=True)
        mirror = work / "repo.git"
        from ghbot.git.mirror import mirror_clone

        await asyncio.to_thread(mirror_clone, s.git, s.backups.url_for(repo.full_name), mirror)
        stats = await asyncio.to_thread(author_stats, s.git, mirror)
    except Exception as exc:  # noqa: BLE001
        await progress.finish(context, update, f"❌ Could not read authors: {safe_error(exc)}")
        return
    finally:
        await asyncio.to_thread(shutil.rmtree, work, True)

    state = {"repo": repo.full_name, "authors": [[a.name, a.email, a.commits] for a in stats[:20]], "selected": []}
    context.user_data["reauthor"] = state  # type: ignore[index]
    await progress.finish(context, update, *(await authors_view(context, state)))


async def authors_view(context: Ctx, state: dict):
    name, email = await target_identity(context)
    selected = {tuple(x) for x in state["selected"]}
    lines = [f"✍️ <b>Commit authors in {h(state['repo'])}</b>", ""]
    rows = []
    for index, (author, mail, count) in enumerate(state["authors"]):
        chosen = (author, mail) in selected
        lines.append(f"{'☑️' if chosen else '⬜'} {h(author)} &lt;{h(mail)}&gt; — {count} commit(s)")
        rows.append([btn(f"{'☑️' if chosen else '⬜'} {author} ({count})"[:60], "ra_pick", index)])
    lines += ["", f"<b>New identity:</b> {h(name)} &lt;{h(email or 'not set')}&gt;",
              "", "Select the identities that are <b>yours</b>. Their commits keep their dates, messages and files; "
              "only the author and committer name/email change.",
              "⚠️ Other people's commits are not yours to relabel: licences usually require keeping attribution."]
    rows.append([btn("✏️ Change new identity", "ra_identity")])
    if selected and email:
        rows.append([btn(f"▶️ Continue with {len(selected)} identity(ies)", "ra_go")])
    rows.append([btn("✖️ Cancel", "ra_cancel")])
    return join_limited(lines), kb(rows)


async def cmd_reauthor(update: Update, context: Ctx) -> None:
    if not context.args:
        raise ValidationError("Usage: /reauthor repo")
    await show_authors(update, context, context.args[0])


@callback("ra_pick")
async def cb_pick(update: Update, context: Ctx, data: tuple) -> None:
    state = context.user_data.get("reauthor")  # type: ignore[union-attr]
    if not state:
        await reply(update, context, "That list expired. Run /reauthor repo again.")
        return
    index = int(data[1])
    if 0 <= index < len(state["authors"]):
        author, mail, _ = state["authors"][index]
        entry = [author, mail]
        if entry in state["selected"]:
            state["selected"].remove(entry)
        else:
            state["selected"].append(entry)
    await reply(update, context, *(await authors_view(context, state)))


@callback("ra_identity")
async def cb_identity(update: Update, context: Ctx, data: tuple) -> None:
    await_input(context, "reauthor_identity")
    await reply(update, context, "✏️ Send the identity commits should get, as <code>Name &lt;email@example.com&gt;</code> "
                                 "or just the email address. /cancel to abort.")


@text_input("reauthor_identity")
async def input_identity(update: Update, context: Ctx, state_in: dict, text: str) -> None:
    match = IDENTITY_RE.match(text.strip())
    if not match:
        raise ValidationError("Send it as <code>Name &lt;email@example.com&gt;</code> or just an email address.")
    s = svc(context)
    explicit = (match["name"] or "").strip()
    name = explicit or s.username
    await s.db.set_setting(s.setting_key("author_name"), name)
    # Only a name you typed yourself is pinned; otherwise it follows the account's username.
    await s.db.set_setting(s.setting_key("author_name_custom"), bool(explicit))
    await s.db.set_setting(s.setting_key("author_email"), match["email"])
    clear_input(context)
    state = context.user_data.get("reauthor")  # type: ignore[union-attr]
    if not state:
        await reply(update, context, f"✅ New identity saved: {code(name + ' <' + match['email'] + '>')}", edit=False)
        return
    await reply(update, context, *(await authors_view(context, state)), edit=False)


@callback("ra_go")
async def cb_go(update: Update, context: Ctx, data: tuple) -> None:
    state = context.user_data.pop("reauthor", None)  # type: ignore[union-attr]
    if not state or not state["selected"]:
        await reply(update, context, "Nothing selected. Run /reauthor repo again.")
        return
    name, email = await target_identity(context)
    if not email:
        await reply(update, context, "Set the new identity first.")
        return
    await start_operation(update, context, "reauthor_repo", repo_ref(context, state["repo"]),
                          {"identities": state["selected"], "new_name": name, "new_email": email})


@callback("ra_cancel")
async def cb_cancel(update: Update, context: Ctx, data: tuple) -> None:
    context.user_data.pop("reauthor", None)  # type: ignore[union-attr]
    clear_input(context)
    await reply(update, context, "✖️ Cancelled. Nothing was changed.")


async def show_identity(update: Update, context: Ctx) -> None:
    s = svc(context)
    name, email = await target_identity(context)
    lines = [f"🪪 <b>Commit identity</b> · account {code(s.active)}", "",
             f"Name: <b>{h(name)}</b>", f"Email: <b>{h(email or 'not set')}</b>",
             f"Source: {h(await identity_source(context))}", "",
             "Used when /reauthor rewrites commits to you. Each account keeps its own identity.",
             "Defaults can also live in .env as <code>GITHUB_EMAIL</code> / <code>GITHUB_NAME</code> "
             "(and <code>GITHUB_EMAIL_2</code> … for further accounts)."]
    rows = [[btn("✏️ Change for this account", "ra_identity")]]
    if await s.db.get_setting(s.setting_key("author_email")):
        rows.append([btn("↩️ Use the .env / username default", "ra_identity_reset")])
    rows.append([btn("👥 Accounts", "accounts")])
    await reply(update, context, "\n".join(lines), kb(rows))


async def cmd_identity(update: Update, context: Ctx) -> None:
    if context.args:
        await input_identity(update, context, {}, " ".join(context.args))
        return
    await show_identity(update, context)


@callback("identity")
async def cb_identity_show(update: Update, context: Ctx, data: tuple) -> None:
    await show_identity(update, context)


@callback("ra_identity_reset")
async def cb_identity_reset(update: Update, context: Ctx, data: tuple) -> None:
    s = svc(context)
    for key in ("author_email", "author_name", "author_name_custom"):
        await s.db.set_setting(s.setting_key(key), None)
    await show_identity(update, context)
