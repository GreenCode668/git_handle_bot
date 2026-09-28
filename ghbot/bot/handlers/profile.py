"""Public GitHub profile: fields, social links and status."""

from __future__ import annotations

import secrets

from telegram import Update

from ghbot.bot.router import await_input, callback, clear_input, text_input
from ghbot.bot.ui import Ctx, btn, code, h, join_limited, kb, reply, safe_error, svc
from ghbot.github.client import GitHubError
from ghbot.services.profile import PROFILE_FIELDS, display, normalize_profile_value, validate_social_url
from ghbot.validators import ValidationError


async def show_profile(update: Update, context: Ctx) -> None:
    s = svc(context)
    user = await s.gh.get_user()
    socials = await s.gh.list_social_accounts()
    try:
        status = await s.gh.get_status()
    except GitHubError:
        status = None
    lines = [f"👤 <b>{h(user['login'])}</b> · <a href=\"{h(user['html_url'])}\">profile</a>", ""]
    for key, spec in PROFILE_FIELDS.items():
        lines.append(f"<b>{h(spec.label)}</b>: {h(display(user.get(key)))}")
    lines.append("")
    lines.append("<b>Social links</b>: " + (", ".join(h(a["url"]) for a in socials) if socials else "—"))
    if status:
        lines.append(f"<b>Status</b>: {h(status.get('emoji') or '')} {h(status.get('message') or '')}"
                     + (" (busy)" if status.get("indicatesLimitedAvailability") else ""))
    else:
        lines.append("<b>Status</b>: —")
    lines.append(f"\nFollowers {user['followers']} · Following {user['following']} · Public repos {user['public_repos']}")
    keys = list(PROFILE_FIELDS)
    rows = [[btn(f"✏️ {PROFILE_FIELDS[k].label}", "prof_edit", k) for k in keys[i:i + 2]] for i in range(0, len(keys), 2)]
    rows.append([btn("➕ Add social link", "prof_social_add"), btn("➖ Remove social link", "prof_social_rm")])
    rows.append([btn("💬 Set status", "prof_status"), btn("🧹 Clear status", "prof_status_clear")])
    rows.append([btn("🔄 Refresh", "prof_show")])
    await reply(update, context, join_limited(lines), kb(rows))


@callback("prof_show")
async def cb_show(update: Update, context: Ctx, data: tuple) -> None:
    await show_profile(update, context)


@callback("prof_edit")
async def cb_edit(update: Update, context: Ctx, data: tuple) -> None:
    key = data[1]
    if key not in PROFILE_FIELDS:
        return
    spec = PROFILE_FIELDS[key]
    await_input(context, "profile_field", field=key)
    await reply(update, context, f"✏️ Send the new <b>{h(spec.label)}</b> ({h(spec.hint)}).\n"
                                 f"Send <code>-</code> to clear it. /cancel to abort.")


@text_input("profile_field")
async def input_field(update: Update, context: Ctx, state: dict, text: str) -> None:
    await propose_profile_change(update, context, state["field"], text)


async def propose_profile_change(update: Update, context: Ctx, key: str, text: str) -> None:
    """Validate, then show old → new and ask for confirmation (nothing is changed yet)."""
    new_value = normalize_profile_value(key, text)
    clear_input(context)
    user = await svc(context).gh.get_user()
    old_value = user.get(key)
    same = old_value == new_value if isinstance(new_value, bool) else (old_value or "") == (new_value or "")
    if same:
        await reply(update, context, "ℹ️ That is already the current value. Nothing to change.")
        return
    nonce = secrets.token_hex(4)
    context.user_data["profile_pending"] = {"nonce": nonce, "field": key, "old": old_value, "new": new_value}  # type: ignore[index]
    label = PROFILE_FIELDS[key].label
    await reply(update, context,
                f"👤 <b>Update {h(label)}</b>\n\nOld: {h(display(old_value))}\nNew: {h(display(new_value))}\n\nApply this change?",
                kb([[btn("✅ Update", "prof_apply", nonce), btn("✖️ Cancel", "prof_show")]]))


@callback("prof_apply")
async def cb_apply(update: Update, context: Ctx, data: tuple) -> None:
    pending = context.user_data.pop("profile_pending", None)  # type: ignore[union-attr]
    if not pending or pending["nonce"] != data[1]:
        await reply(update, context, "This confirmation expired. Start again from 👤 Profile.")
        return
    s = svc(context)
    key = pending["field"]
    try:
        updated = await s.gh.update_user(**{key: pending["new"]})
    except GitHubError as exc:
        await s.db.log_simple("profile_update", None, "failed", params={"field": key}, error=str(exc))
        await reply(update, context, f"❌ GitHub rejected the update: {safe_error(exc)}")
        return
    verified = (updated.get(key) or "") == (pending["new"] or "") or updated.get(key) == pending["new"]
    await s.db.log_simple("profile_update", None, "done" if verified else "failed",
                          params={"field": key}, result={"verified": verified})
    await reply(update, context, (f"✅ {h(PROFILE_FIELDS[key].label)} updated and verified." if verified
                                  else f"⚠️ GitHub accepted the request but returned {h(display(updated.get(key)))}."),
                kb([[btn("👤 Profile", "prof_show")]]))


@callback("prof_social_add")
async def cb_social_add(update: Update, context: Ctx, data: tuple) -> None:
    await_input(context, "social_add")
    await reply(update, context, "➕ Send the full https:// URL of the social profile to add. /cancel to abort.")


@text_input("social_add")
async def input_social_add(update: Update, context: Ctx, state: dict, text: str) -> None:
    url = validate_social_url(text)
    clear_input(context)
    nonce = secrets.token_hex(4)
    context.user_data["social_pending"] = {"nonce": nonce, "url": url, "op": "add"}  # type: ignore[index]
    await reply(update, context, f"Add social link {code(url)}?",
                kb([[btn("✅ Add", "prof_social_apply", nonce), btn("✖️ Cancel", "prof_show")]]))


@callback("prof_social_rm")
async def cb_social_rm(update: Update, context: Ctx, data: tuple) -> None:
    socials = await svc(context).gh.list_social_accounts()
    if not socials:
        await reply(update, context, "No social links to remove.", kb([[btn("👤 Profile", "prof_show")]]))
        return
    context.user_data["social_choices"] = [a["url"] for a in socials]  # type: ignore[index]
    rows = [[btn(f"➖ {a['url']}"[:60], "prof_social_pick", i)] for i, a in enumerate(socials)]
    rows.append([btn("⬅️ Profile", "prof_show")])
    await reply(update, context, "Choose a link to remove:", kb(rows))


@callback("prof_social_pick")
async def cb_social_pick(update: Update, context: Ctx, data: tuple) -> None:
    choices = context.user_data.get("social_choices") or []  # type: ignore[union-attr]
    index = int(data[1])
    if not 0 <= index < len(choices):
        return
    nonce = secrets.token_hex(4)
    context.user_data["social_pending"] = {"nonce": nonce, "url": choices[index], "op": "remove"}  # type: ignore[index]
    await reply(update, context, f"Remove social link {code(choices[index])}?",
                kb([[btn("✅ Remove", "prof_social_apply", nonce), btn("✖️ Cancel", "prof_show")]]))


@callback("prof_social_apply")
async def cb_social_apply(update: Update, context: Ctx, data: tuple) -> None:
    pending = context.user_data.pop("social_pending", None)  # type: ignore[union-attr]
    if not pending or pending["nonce"] != data[1]:
        await reply(update, context, "This confirmation expired.")
        return
    s = svc(context)
    try:
        if pending["op"] == "add":
            await s.gh.add_social_accounts([pending["url"]])
        else:
            await s.gh.delete_social_accounts([pending["url"]])
    except GitHubError as exc:
        await reply(update, context, f"❌ {safe_error(exc)}")
        return
    present = pending["url"] in {a["url"] for a in await s.gh.list_social_accounts()}
    ok = present if pending["op"] == "add" else not present
    await s.db.log_simple(f"social_{pending['op']}", None, "done" if ok else "failed", params={"url": pending["url"]})
    await reply(update, context, "✅ Done and verified." if ok else "⚠️ GitHub did not reflect the change yet.",
                kb([[btn("👤 Profile", "prof_show")]]))


@callback("prof_status")
async def cb_status(update: Update, context: Ctx, data: tuple) -> None:
    await_input(context, "status")
    await reply(update, context, "💬 Send your status as <code>:emoji: message</code>, e.g. <code>:rocket: Shipping</code> "
                                 "(max 80 chars). /cancel to abort.")


@text_input("status")
async def input_status(update: Update, context: Ctx, state: dict, text: str) -> None:
    text = text.strip()
    emoji = None
    if text.startswith(":") and text.count(":") >= 2:
        emoji, _, text = text[1:].partition(":")
        emoji = f":{emoji}:"
        text = text.strip()
    if len(text) > 80 or not text:
        raise ValidationError("Status message must be 1-80 characters.")
    clear_input(context)
    nonce = secrets.token_hex(4)
    context.user_data["status_pending"] = {"nonce": nonce, "emoji": emoji, "message": text}  # type: ignore[index]
    await reply(update, context, f"Set status to {h(emoji or '')} {h(text)}?",
                kb([[btn("✅ Set", "prof_status_apply", nonce), btn("✖️ Cancel", "prof_show")]]))


@callback("prof_status_clear")
async def cb_status_clear(update: Update, context: Ctx, data: tuple) -> None:
    nonce = secrets.token_hex(4)
    context.user_data["status_pending"] = {"nonce": nonce, "emoji": None, "message": None}  # type: ignore[index]
    await reply(update, context, "Clear your GitHub status?",
                kb([[btn("✅ Clear", "prof_status_apply", nonce), btn("✖️ Cancel", "prof_show")]]))


@callback("prof_status_apply")
async def cb_status_apply(update: Update, context: Ctx, data: tuple) -> None:
    pending = context.user_data.pop("status_pending", None)  # type: ignore[union-attr]
    if not pending or pending["nonce"] != data[1]:
        await reply(update, context, "This confirmation expired.")
        return
    s = svc(context)
    try:
        await s.gh.set_status(pending["message"], pending["emoji"])
    except GitHubError as exc:
        await reply(update, context, f"❌ {safe_error(exc)}")
        return
    await s.db.log_simple("profile_status", None, "done")
    await show_profile(update, context)


async def cmd_profile(update: Update, context: Ctx) -> None:
    await show_profile(update, context)


async def _field_shortcut(update: Update, context: Ctx, key: str, usage: str) -> None:
    if not context.args:
        await cb_edit(update, context, ("prof_edit", key))
        return
    raw = update.effective_message.text.split(maxsplit=1)[1] if update.effective_message else " ".join(context.args)  # type: ignore[union-attr]
    await propose_profile_change(update, context, key, raw)


async def cmd_bio(update: Update, context: Ctx) -> None:
    await _field_shortcut(update, context, "bio", "/bio text")


async def cmd_name(update: Update, context: Ctx) -> None:
    await _field_shortcut(update, context, "name", "/name text")


async def cmd_location(update: Update, context: Ctx) -> None:
    await _field_shortcut(update, context, "location", "/location text")


async def cmd_website(update: Update, context: Ctx) -> None:
    await _field_shortcut(update, context, "blog", "/website url")


async def cmd_socials(update: Update, context: Ctx) -> None:
    socials = await svc(context).gh.list_social_accounts()
    lines = ["🔗 <b>Social links</b>", ""]
    lines += [f"• {h(a.get('provider', 'generic'))}: {h(a['url'])}" for a in socials] or ["No social links configured."]
    await reply(update, context, "\n".join(lines),
                kb([[btn("➕ Add", "prof_social_add"), btn("➖ Remove", "prof_social_rm")], [btn("👤 Profile", "prof_show")]]))


async def cmd_social_add(update: Update, context: Ctx) -> None:
    if context.args:
        await input_social_add(update, context, {}, context.args[0])
        return
    await cb_social_add(update, context, ())


async def cmd_social_remove(update: Update, context: Ctx) -> None:
    await cb_social_rm(update, context, ())
