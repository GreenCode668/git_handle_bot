"""Several GitHub accounts: list them and switch the active one."""

from __future__ import annotations

from telegram import Update

from ghbot.bot.router import callback
from ghbot.bot.ui import Ctx, btn, code, h, join_limited, kb, reply, svc
from ghbot.github.client import GitHubError
from ghbot.services.safety import RUNNING, WAITING
from ghbot.validators import ValidationError


async def blocking_operations(context: Ctx) -> list:
    return await svc(context).db.operations_in_stages([*WAITING, *RUNNING])


async def show_accounts(update: Update, context: Ctx) -> None:
    s = svc(context)
    lines = ["👥 <b>GitHub accounts</b>", ""]
    rows = []
    for username in s.usernames:
        active = username == s.active
        account = s.settings.account(username)
        identity = await s.db.get_setting(f"author_email:{username.lower()}") or (account.email if account else None)
        name = (account.display_name if account else None) or username
        lines.append(f"{'🟢' if active else '⚪'} {code(username)}" + (" — active" if active else ""))
        lines.append(f"     commits as {h(name)} &lt;{h(identity or 'no email set')}&gt;")
        if not active:
            rows.append([btn(f"🔁 Switch to {username}", "acct_switch", username)])
    if len(s.usernames) == 1:
        lines += ["", "Only one account is configured. Add more in <code>.env</code>:",
                  "<code>GITHUB_USERNAME_2=other-account</code>", "<code>GITHUB_TOKEN_2=ghp_…</code>",
                  "<code>GITHUB_EMAIL_2=other@example.com</code> (optional commit identity)",
                  "then restart the bot. Tokens stay in .env and never pass through Telegram."]
    else:
        lines += ["", "Everything (repositories, backups, operations) applies to the active account.",
                  "Backups keep the owner in their name, so they stay linked to the account they came from.",
                  "Each account keeps its own commit identity (/identity)."]
    pending = await blocking_operations(context)
    if pending:
        lines.append(f"\n⏳ {len(pending)} operation(s) are pending: switching is blocked until they finish "
                     "or are cancelled (/pending).")
    rows.append([btn("🪪 Commit identity", "identity"), btn("🔄 Refresh", "accounts"), btn("🩺 Status", "status")])
    await reply(update, context, join_limited(lines), kb(rows))


async def cmd_accounts(update: Update, context: Ctx) -> None:
    if context.args:
        await switch_to(update, context, context.args[0])
        return
    await show_accounts(update, context)


@callback("accounts")
async def cb_accounts(update: Update, context: Ctx, data: tuple) -> None:
    await show_accounts(update, context)


@callback("acct_switch")
async def cb_switch(update: Update, context: Ctx, data: tuple) -> None:
    await switch_to(update, context, data[1])


async def cmd_switch(update: Update, context: Ctx) -> None:
    if not context.args:
        await show_accounts(update, context)
        return
    await switch_to(update, context, context.args[0])


async def switch_to(update: Update, context: Ctx, username: str) -> None:
    s = svc(context)
    pending = await blocking_operations(context)
    if pending:
        raise ValidationError(
            f"{len(pending)} operation(s) are still pending for {s.active}. Finish or cancel them first (/pending)."
        )
    try:
        active = await s.switch(username)
    except KeyError:
        raise ValidationError(f"{username} is not configured. Add it to .env as GITHUB_USERNAME_2/GITHUB_TOKEN_2.") from None
    context.user_data.clear()  # type: ignore[union-attr]
    detail = ""
    try:
        user = await s.gh.get_user()
        detail = f" · {user['public_repos']} public repositories"
        if user["login"].lower() != active.lower():
            detail = f" ⚠️ this token belongs to {h(user['login'])}, not {h(active)}"
    except GitHubError as exc:
        detail = f" ⚠️ {h(exc)}"
    await s.db.log_simple("switch_account", None, "done", params={"account": active})
    await reply(update, context, f"🟢 Active account: {code(active)}{detail}\nPending selections were cleared.",
                kb([[btn("📁 Repositories", "repos", "detail", 1), btn("👥 Accounts", "accounts")]]))
