"""Badge management in a dedicated section of the profile README."""

from __future__ import annotations

import secrets

from telegram import Update

from ghbot.bot.router import await_input, callback, clear_input, text_input
from ghbot.bot.ui import Ctx, ProgressMessage, btn, code, h, join_limited, kb, reply, safe_error, svc
from ghbot.github.client import GitHubError
from ghbot.services.badges import (
    TECH_PRESETS,
    Badge,
    apply_section,
    build_badge_config,
    outside_section,
    render_badge,
    render_section,
)
from ghbot.validators import ValidationError

KIND_LABELS = {
    "stats": "📈 Stats card", "top_langs": "🧮 Top languages", "streak": "🔥 Streak",
    "followers": "👥 Followers", "stars": "⭐ Stars", "views": "👀 Profile views",
}


def profile_repo(context: Ctx) -> str:
    user = svc(context).username
    return f"{user}/{user}"


async def _badges(context: Ctx) -> list[Badge]:
    return [Badge(b["id"], b["kind"], b["label"], b["config"]) for b in await svc(context).db.list_badges()]


async def show_badges(update: Update, context: Ctx) -> None:
    badges = await _badges(context)
    lines = ["🏅 <b>Profile badges</b>",
             f"Managed only inside a marked section of {code(profile_repo(context) + '/README.md')}. "
             "Other README content is never modified.", ""]
    lines += [f"{i}. {h(b.label)} <i>({h(b.kind)})</i>" for i, b in enumerate(badges, 1)] or ["No badges yet."]
    rows = [
        [btn("➕ Technology", "badge_tech"), btn("➕ Custom (Shields.io)", "badge_custom")],
        [btn(label, "badge_add", kind) for kind, label in list(KIND_LABELS.items())[:3]],
        [btn(label, "badge_add", kind) for kind, label in list(KIND_LABELS.items())[3:]],
        [btn("↕️ Reorder / 🗑 Remove", "badge_manage"), btn("👁 Preview", "badge_preview")],
        [btn("🚀 Publish to README", "badge_publish_ask"), btn("↩️ Revert last publish", "badge_revert_ask")],
    ]
    await reply(update, context, join_limited(lines), kb(rows))


@callback("badges")
async def cb_badges(update: Update, context: Ctx, data: tuple) -> None:
    await show_badges(update, context)


@callback("badge_tech")
async def cb_tech(update: Update, context: Ctx, data: tuple) -> None:
    names = list(TECH_PRESETS)
    rows = [[btn(n, "badge_tech_add", n) for n in names[i:i + 3]] for i in range(0, len(names), 3)]
    rows.append([btn("✍️ Other…", "badge_tech_other"), btn("⬅️ Badges", "badges")])
    await reply(update, context, "Choose a technology:", kb(rows))


@callback("badge_tech_add")
async def cb_tech_add(update: Update, context: Ctx, data: tuple) -> None:
    await _add(update, context, "tech", name=data[1])


@callback("badge_tech_other")
async def cb_tech_other(update: Update, context: Ctx, data: tuple) -> None:
    await_input(context, "badge_tech")
    await reply(update, context, "Send <code>Name | logo-slug | HEXCOLOR</code> (logo from simpleicons.org), "
                                 "e.g. <code>Svelte | svelte | FF3E00</code>. /cancel to abort.")


@text_input("badge_tech")
async def input_tech(update: Update, context: Ctx, state: dict, text: str) -> None:
    parts = [p.strip() for p in text.split("|")]
    if not 1 <= len(parts) <= 3:
        raise ValidationError("Format: Name | logo-slug | HEXCOLOR")
    clear_input(context)
    await _add(update, context, "tech", name=parts[0], logo=parts[1] if len(parts) > 1 else "",
               color=parts[2] if len(parts) > 2 else "")


@callback("badge_custom")
async def cb_custom(update: Update, context: Ctx, data: tuple) -> None:
    await_input(context, "badge_custom")
    await reply(update, context, "Send <code>label | message | color</code>, e.g. <code>status | open to work | brightgreen</code>. "
                                 "/cancel to abort.")


@text_input("badge_custom")
async def input_custom(update: Update, context: Ctx, state: dict, text: str) -> None:
    parts = [p.strip() for p in text.split("|")]
    if len(parts) not in (2, 3):
        raise ValidationError("Format: label | message | color")
    clear_input(context)
    await _add(update, context, "custom", label=parts[0], message=parts[1], color=parts[2] if len(parts) > 2 else "")


@callback("badge_add")
async def cb_add(update: Update, context: Ctx, data: tuple) -> None:
    if data[1] in KIND_LABELS:
        await _add(update, context, data[1])


async def _add(update: Update, context: Ctx, kind: str, **opts: str) -> None:
    s = svc(context)
    label, config = build_badge_config(kind, s.username, **opts)
    await s.db.add_badge(kind, label, config)
    await reply(update, context, f"✅ Added badge <b>{h(label)}</b> (not published yet).",
                kb([[btn("🏅 Badges", "badges"), btn("🚀 Publish", "badge_publish_ask")]]))


@callback("badge_manage")
async def cb_manage(update: Update, context: Ctx, data: tuple) -> None:
    badges = await _badges(context)
    rows = [[btn("⬆️", "badge_move", b.id, -1), btn("⬇️", "badge_move", b.id, 1), btn(f"🗑 {b.label}"[:40], "badge_del", b.id)]
            for b in badges]
    rows.append([btn("⬅️ Badges", "badges")])
    await reply(update, context, "Reorder or remove badges (changes apply on next publish):" if badges else "No badges.", kb(rows))


@callback("badge_move")
async def cb_move(update: Update, context: Ctx, data: tuple) -> None:
    await svc(context).db.move_badge(int(data[1]), int(data[2]))
    await cb_manage(update, context, data)


@callback("badge_del")
async def cb_del(update: Update, context: Ctx, data: tuple) -> None:
    await svc(context).db.delete_badge(int(data[1]))
    await cb_manage(update, context, data)


@callback("badge_preview")
async def cb_preview(update: Update, context: Ctx, data: tuple) -> None:
    section = render_section(await _badges(context))
    await reply(update, context, f"👁 <b>Managed section preview</b>\n<pre>{h(section)[:3500]}</pre>",
                kb([[btn("⬅️ Badges", "badges")]]))


@callback("badge_publish_ask")
async def cb_publish_ask(update: Update, context: Ctx, data: tuple) -> None:
    s = svc(context)
    repo = profile_repo(context)
    if not await s.gh.repo_exists(repo):
        await reply(update, context, f"🛑 Profile README repository {code(repo)} does not exist. Create a public repository "
                                     f"named {code(s.username)} on GitHub first.")
        return
    current = await s.gh.get_file(repo, "README.md")
    old_text, sha = current if current else ("", None)
    new_text = apply_section(old_text, await _badges(context))
    if new_text == old_text:
        await reply(update, context, "ℹ️ README already up to date.", kb([[btn("⬅️ Badges", "badges")]]))
        return
    nonce = secrets.token_hex(4)
    context.user_data["badge_publish"] = {"nonce": nonce, "sha": sha, "old": old_text, "new": new_text}  # type: ignore[index]
    untouched = len(outside_section(old_text))
    await reply(update, context,
                f"🚀 <b>Publish badges</b> to {code(repo + '/README.md')}\n"
                f"{'README exists' if current else 'README will be created'} · content outside the section: {untouched} chars (unchanged)\n"
                f"Section size: {len(render_section(await _badges(context)))} chars\n\n"
                "The previous README is saved so it can be reverted.",
                kb([[btn("✅ Publish", "badge_publish", nonce), btn("✖️ Cancel", "badges")]]))


@callback("badge_publish")
async def cb_publish(update: Update, context: Ctx, data: tuple) -> None:
    pending = context.user_data.pop("badge_publish", None)  # type: ignore[union-attr]
    if not pending or pending["nonce"] != data[1]:
        await reply(update, context, "This confirmation expired. Start again.")
        return
    s = svc(context)
    repo = profile_repo(context)
    progress = await ProgressMessage.create(update, context, "🚀 Publishing…")
    await s.db.save_readme_snapshot(repo, "README.md", pending["sha"], pending["old"])
    try:
        await s.gh.put_file(repo, "README.md", pending["new"], "Update profile badges via Telegram bot", pending["sha"])
    except GitHubError as exc:
        hint = " README changed on GitHub meanwhile; nothing was overwritten. Try again." if exc.status in (409, 422) else ""
        await s.db.log_simple("badges_publish", repo, "failed", error=str(exc))
        await progress.finish(context, update, f"❌ {safe_error(exc)}{hint}")
        return
    fetched = await s.gh.get_file(repo, "README.md")
    ok = fetched is not None and fetched[0] == pending["new"]
    await s.db.log_simple("badges_publish", repo, "done" if ok else "failed", result={"verified": ok})
    await progress.finish(context, update, "✅ Badges published and verified." if ok else "⚠️ Published, but verification differed.",
                          kb([[btn("🏅 Badges", "badges")]]))


@callback("badge_revert_ask")
async def cb_revert_ask(update: Update, context: Ctx, data: tuple) -> None:
    snapshot = await svc(context).db.latest_readme_snapshot(profile_repo(context))
    if snapshot is None:
        await reply(update, context, "No previous README snapshot saved.", kb([[btn("⬅️ Badges", "badges")]]))
        return
    await reply(update, context, f"↩️ Restore README from snapshot taken {h(snapshot['created_at'])}?",
                kb([[btn("✅ Restore", "badge_revert", snapshot["id"]), btn("✖️ Cancel", "badges")]]))


@callback("badge_revert")
async def cb_revert(update: Update, context: Ctx, data: tuple) -> None:
    s = svc(context)
    repo = profile_repo(context)
    snapshot = await s.db.latest_readme_snapshot(repo)
    if snapshot is None or snapshot["id"] != int(data[1]):
        await reply(update, context, "Snapshot changed; start again.")
        return
    current = await s.gh.get_file(repo, "README.md")
    try:
        await s.gh.put_file(repo, "README.md", snapshot["content"], "Revert profile badges via Telegram bot",
                            current[1] if current else None)
    except GitHubError as exc:
        await reply(update, context, f"❌ {safe_error(exc)}")
        return
    await s.db.log_simple("badges_revert", repo, "done")
    await reply(update, context, "✅ README restored.", kb([[btn("🏅 Badges", "badges")]]))


async def cmd_badges(update: Update, context: Ctx) -> None:
    await show_badges(update, context)


__all__ = ["cmd_badges", "show_badges", "render_badge"]
