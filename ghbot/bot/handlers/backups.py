"""Backup menu: create, list, details, verify, restore, delete, undo."""

from __future__ import annotations

from telegram import Update

from ghbot.bot.handlers.operations import start_operation
from ghbot.bot.handlers.repos import repo_ref, show_repo_list
from ghbot.bot.router import await_input, callback, clear_input, text_input
from ghbot.bot.ui import Ctx, ProgressMessage, ago, btn, code, fmt_bytes, h, join_limited, kb, pager, reply, safe_error, svc
from ghbot.services.backups import BackupError
from ghbot.services.safety import RUNNING, WAITING, Stage
from ghbot.validators import RepoRef, ValidationError, confirmation_phrase, matches_confirmation, validate_backup_id

STATUS_EMOJI = {"verified": "✅", "created": "🟡", "creating": "⏳", "failed": "❌", "deleted": "🗑"}
PURPOSE_TITLES = {"details": "🔎 Backup details", "verify": "✅ Verify which backup?", "restore": "♻️ Restore which backup?",
                  "delete": "🗑 Delete which backup?", "list": "📋 Backups"}


async def show_backup_menu(update: Update, context: Ctx) -> None:
    s = svc(context)
    _, total = await s.db.list_backups(limit=1, public_only=not s.policy.show_private)
    auto = await s.db.get_setting("autobackup_enabled", False)
    await reply(update, context, f"💾 <b>Backups</b>\nStored backups: {total}\nAutomatic backups: {'on' if auto else 'off'}\n"
                                 f"Path: {code(s.settings.backup_path)}",
                kb([[btn("💾 Create Backup", "pick_backup_repo"), btn("📋 List Backups", "backups", "list", 1)],
                    [btn("🔎 Backup Details", "backups", "details", 1), btn("♻️ Restore", "backups", "restore", 1)],
                    [btn("✅ Verify Backup", "backups", "verify", 1), btn("🗑 Delete Backup", "backups", "delete", 1)],
                    [btn("📦 Backup all", "backup_all_ask"), btn("🧪 Verify all", "verify_all_ask")],
                    [btn("🤖 Automatic backups", "autobackup"), btn("📊 Storage status", "backup_status")],
                    [btn("📖 Guide", "guide", "backups")]]))


@callback("pick_backup_repo")
async def cb_pick_backup_repo(update: Update, context: Ctx, data: tuple) -> None:
    await show_repo_list(update, context, "backup", 1)


@callback("backups")
async def cb_backups(update: Update, context: Ctx, data: tuple) -> None:
    purpose, page = data[1], int(data[2])
    per_page = 8
    s = svc(context)
    records, total = await s.db.list_backups(limit=per_page, offset=(page - 1) * per_page,
                                             public_only=not s.policy.show_private)
    lines = [f"<b>{PURPOSE_TITLES.get(purpose, 'Backups')}</b> ({total})", ""]
    rows = []
    for r in records:
        lines.append(f"{STATUS_EMOJI.get(r.status, '•')} {code(r.id)} {h(r.repo)} · {ago(r.created_at)} · {fmt_bytes(r.size_bytes)}")
        target = {"verify": "bk_verify", "restore": "bk_restore", "delete": "bk_delete"}.get(purpose, "bk")
        rows.append([btn(f"{STATUS_EMOJI.get(r.status, '•')} {r.id} {r.repo.split('/')[-1]}"[:60], target, r.id)])
    if not records:
        lines.append("No backups yet.")
    last = max(1, -(-total // per_page))
    rows.append(pager("backups", page, page < last, purpose, last_page=last))
    rows.append([btn("⬅️ Backup menu", "backup_menu")])
    await reply(update, context, join_limited(lines), kb(rows))


@callback("backup_menu")
async def cb_backup_menu(update: Update, context: Ctx, data: tuple) -> None:
    await show_backup_menu(update, context)


async def create_backup(update: Update, context: Ctx, repo: RepoRef) -> None:
    s = svc(context)
    meta = await s.policy.readable(repo)
    s.policy.check_writable_meta(meta)  # backups are for managed (owned + public) repositories
    repo = RepoRef(repo.owner, meta["name"])
    try:
        await s.db.acquire_lock(repo.key, "manual backup")
    except Exception as exc:  # noqa: BLE001
        await reply(update, context, f"🔒 {safe_error(exc)}")
        return
    progress = await ProgressMessage.create(update, context, f"💾 Creating mirror backup of {code(repo)}…")
    try:
        record = await s.backups.create(repo, "manual")
        await progress(f"🔎 Verifying {code(record.id)}…")
        report = await s.backups.verify(record.id)
    except BackupError as exc:
        await s.db.log_simple("backup_create", repo.full_name, "failed", error=str(exc))
        await progress.finish(context, update, f"❌ {safe_error(exc)}")
        return
    finally:
        await s.db.release_lock(repo.key)
    stage = "done" if report.ok else "failed"
    await s.db.log_simple("backup_create", repo.full_name, stage, result={"backup_id": record.id, "verified": report.ok})
    text = await backup_details_text(context, record.id)
    await progress.finish(context, update, text, backup_buttons(record.id))


async def backup_details_text(context: Ctx, backup_id: str) -> str:
    details = await svc(context).backups.details(backup_id)
    if details is None:
        return "Backup not found."
    r, m = details.record, details.manifest or {}
    lines = [
        f"{STATUS_EMOJI.get(r.status, '•')} <b>{h(r.id)}</b> — {h(r.status)}",
        f"Repository: {code(r.repo)}",
        f"Reason: {h(r.reason or '—')}",
        f"Created: {ago(r.created_at)} UTC · Verified: {ago(r.verified_at)}",
        f"Refs: {r.ref_count} · Commits: {r.commit_count} · Size: {fmt_bytes(r.size_bytes)}",
    ]
    if m:
        heads = sum(1 for k in m.get("refs", {}) if k.startswith("refs/heads/"))
        tags = sum(1 for k in m.get("refs", {}) if k.startswith("refs/tags/"))
        lines.append(f"Branches: {heads} · Tags: {tags} · HEAD: {code(m.get('head'))}")
        lines.append(f"Includes: {h(', '.join(m.get('includes', [])))}")
        lines.append(f"Not included: {h(', '.join(m.get('excludes', [])))}")
        lines += [f"⚠️ {h(w)}" for w in m.get("warnings", [])]
    if r.bundle_sha256:
        lines.append(f"Bundle SHA-256: {code(r.bundle_sha256[:16])}…")
    if r.error:
        lines.append(f"Error: {h(r.error)}")
    return join_limited(lines)


def backup_buttons(backup_id: str):
    return kb([[btn("✅ Verify", "bk_verify", backup_id), btn("♻️ Restore", "bk_restore", backup_id)],
               [btn("🔍 Diff with GitHub", "bk_diff", backup_id), btn("🗑 Delete", "bk_delete", backup_id)],
               [btn("⬅️ Backups", "backups", "list", 1)]])


@callback("bk")
async def cb_backup_detail(update: Update, context: Ctx, data: tuple) -> None:
    backup_id = validate_backup_id(data[1])
    await reply(update, context, await backup_details_text(context, backup_id), backup_buttons(backup_id))


@callback("bk_verify")
async def cb_verify(update: Update, context: Ctx, data: tuple) -> None:
    backup_id = validate_backup_id(data[1])
    progress = await ProgressMessage.create(update, context, f"🔎 Verifying {code(backup_id)} (fsck, refs, bundle restore test)…")
    try:
        report = await svc(context).backups.verify(backup_id)
    except BackupError as exc:
        await progress.finish(context, update, f"❌ {safe_error(exc)}")
        return
    lines = [f"{'✅' if report.ok else '❌'} <b>Verification {'passed' if report.ok else 'FAILED'}</b> for {code(backup_id)}", ""]
    lines += [f"{'✅' if ok else '❌'} {h(name)}{(': ' + h(detail)) if detail else ''}" for name, ok, detail in report.checks]
    await svc(context).db.log_simple("backup_verify", None, "done" if report.ok else "failed", params={"backup_id": backup_id})
    await progress.finish(context, update, join_limited(lines), backup_buttons(backup_id))


@callback("bk_restore")
async def cb_restore(update: Update, context: Ctx, data: tuple) -> None:
    backup_id = validate_backup_id(data[1])
    record = await svc(context).db.get_backup(backup_id)
    if record is None or record.status == "deleted":
        await reply(update, context, "Backup not available.")
        return
    await start_operation(update, context, "restore_backup", repo_ref(context, record.repo), {"backup_id": backup_id})


@callback("bk_delete")
async def cb_delete(update: Update, context: Ctx, data: tuple) -> None:
    backup_id = validate_backup_id(data[1])
    s = svc(context)
    record = await s.db.get_backup(backup_id)
    if record is None or record.status == "deleted":
        await reply(update, context, "Backup not available.")
        return
    busy = [op for op in await s.db.operations_in_stages([*WAITING, *RUNNING])
            if op.backup_id == backup_id or op.params.get("backup_id") == backup_id]
    if busy:
        await reply(update, context, f"🔒 {code(backup_id)} is used by pending operation #{busy[0].id}. Cancel it first.")
        return
    latest = await s.db.latest_verified_backup(record.repo)
    warn = "\n⚠️ This is the <b>latest verified backup</b> for this repository." if latest and latest.id == backup_id else ""
    phrase = confirmation_phrase("DELETE", backup_id)
    await_input(context, "delete_backup", backup_id=backup_id, phrase=phrase)
    await reply(update, context, f"🗑 Delete backup {code(backup_id)} of {code(record.repo)}? This cannot be undone.{warn}\n\n"
                                 f"Send exactly {code(phrase)} to confirm, or /cancel.")


@text_input("delete_backup")
async def input_delete_backup(update: Update, context: Ctx, state: dict, text: str) -> None:
    clear_input(context)
    if not matches_confirmation(text, state["phrase"]):
        await reply(update, context, "✖️ Phrase did not match. Backup was not deleted.")
        return
    try:
        await svc(context).backups.delete(state["backup_id"])
    except BackupError as exc:
        await reply(update, context, f"❌ {safe_error(exc)}")
        return
    await svc(context).db.log_simple("backup_delete", None, "done", params={"backup_id": state["backup_id"]})
    await reply(update, context, f"🗑 Backup {code(state['backup_id'])} deleted.")


async def cmd_backup(update: Update, context: Ctx) -> None:
    if not context.args:
        await show_repo_list(update, context, "backup", 1)
        return
    await create_backup(update, context, repo_ref(context, context.args[0]))


async def cmd_backups(update: Update, context: Ctx) -> None:
    if context.args:
        backup_id = validate_backup_id(context.args[0])
        await reply(update, context, await backup_details_text(context, backup_id), backup_buttons(backup_id))
        return
    await cb_backups(update, context, ("backups", "list", 1))


async def cmd_restore(update: Update, context: Ctx) -> None:
    args = context.args or []
    if not args:
        await cb_backups(update, context, ("backups", "restore", 1))
        return
    backup_id = validate_backup_id(args[0])
    record = await svc(context).db.get_backup(backup_id)
    if record is None or record.status == "deleted":
        raise ValidationError("Backup not found.")
    target = repo_ref(context, args[1]) if len(args) > 1 else repo_ref(context, record.repo)
    await start_operation(update, context, "restore_backup", target, {"backup_id": backup_id})


async def cmd_undo(update: Update, context: Ctx) -> None:
    s = svc(context)
    last = await s.db.last_operation_with_backup(
        ["delete_repo", "rewrite_history", "restore_backup"], [Stage.DONE, Stage.FAILED, Stage.INTERRUPTED]
    )
    if last is not None and last.repo:
        record = await s.db.get_backup(last.backup_id or "")
        if record and record.status == "verified":
            await reply(update, context, f"↩️ Undo operation #{last.id} ({h(last.kind)} on {code(last.repo)}) by restoring "
                                         f"{code(record.id)}. Nothing happens without your confirmation.", edit=False)
            await start_operation(update, context, "restore_backup", repo_ref(context, last.repo), {"backup_id": record.id})
            return
    latest = await s.db.latest_verified_backup(public_only=True)
    if latest is None:
        await reply(update, context, "No valid backup is available to undo with.")
        return
    await reply(update, context, f"↩️ No recent destructive operation found. Latest verified backup is {code(latest.id)} "
                                 f"for {code(latest.repo)}.", edit=False)
    await start_operation(update, context, "restore_backup", repo_ref(context, latest.repo), {"backup_id": latest.id})


# ------------------------------------------------------------ bulk operations
def _bulk_lines(title: str, result) -> list[str]:
    lines = [title, result.summary(), ""]
    for label, items in (("✅ Created", result.created), ("🟰 Unchanged", result.unchanged),
                         ("⏭ Skipped", result.skipped), ("❌ Failed", result.failed), ("🧹 Pruned", result.pruned)):
        if items:
            lines.append(f"<b>{label}</b>")
            lines += [f"• {h(i)}" for i in items[:30]]
    return lines


@callback("backup_all_ask")
async def cb_backup_all_ask(update: Update, context: Ctx, data: tuple) -> None:
    s = svc(context)
    repos = await s.bulk.managed_repos()
    names = ", ".join(r["name"] for r in repos[:25]) + (" …" if len(repos) > 25 else "")
    await reply(update, context,
                f"📦 <b>Back up all managed public repositories</b>\nRepositories: {len(repos)}\n{h(names)}\n\n"
                "Each backup is a full mirror and is verified. Locked repositories are skipped.\nStart?",
                kb([[btn("✅ Only changed repos", "backup_all_run", True), btn("✅ All repos", "backup_all_run", False)],
                    [btn("✖️ Cancel", "backup_menu")]]))


@callback("backup_all_run")
async def cb_backup_all_run(update: Update, context: Ctx, data: tuple) -> None:
    s = svc(context)
    if s.bulk.busy:
        await reply(update, context, "⏳ A bulk backup is already running.")
        return
    progress = await ProgressMessage.create(update, context, "📦 Starting bulk backup…")
    try:
        repos = await s.bulk.managed_repos()
        result = await s.bulk.run(repos, reason="backup-all", skip_unchanged=bool(data[1]), progress=progress)
    except BackupError as exc:
        await progress.finish(context, update, f"❌ {safe_error(exc)}")
        return
    await s.db.log_simple("backup_all", None, "done" if not result.failed else "failed", result={"summary": result.summary()})
    await progress.finish(context, update, join_limited(_bulk_lines("📦 <b>Bulk backup finished</b>", result)),
                          kb([[btn("⬅️ Backup menu", "backup_menu")]]))


async def cmd_backup_all(update: Update, context: Ctx) -> None:
    await cb_backup_all_ask(update, context, ())


@callback("verify_all_ask")
async def cb_verify_all_ask(update: Update, context: Ctx, data: tuple) -> None:
    _, total = await svc(context).db.list_backups(limit=1)
    await reply(update, context, f"🧪 Verify all {total} backups (fsck, refs, bundle restore test)? This may take a while.",
                kb([[btn("✅ Verify all", "verify_all_run"), btn("✖️ Cancel", "backup_menu")]]))


@callback("verify_all_run")
async def cb_verify_all_run(update: Update, context: Ctx, data: tuple) -> None:
    s = svc(context)
    progress = await ProgressMessage.create(update, context, "🧪 Verifying backups…")
    report = await s.bulk.verify_all(progress)
    lines = ["🧪 <b>Verification report</b>", f"✅ OK: {len(report['ok'])} · ❌ Damaged: {len(report['damaged'])} · "
             f"❓ Missing: {len(report['missing'])}", ""]
    if report["damaged"]:
        lines += ["<b>Damaged</b>"] + [f"• {h(x)}" for x in report["damaged"][:30]]
    if report["missing"]:
        lines += ["<b>Missing</b>"] + [f"• {h(x)}" for x in report["missing"][:30]]
    await s.db.log_simple("verify_all", None, "done" if not (report["damaged"] or report["missing"]) else "failed",
                          result={k: len(v) for k, v in report.items()})
    await progress.finish(context, update, join_limited(lines), kb([[btn("⬅️ Backup menu", "backup_menu")]]))


async def cmd_verify_all(update: Update, context: Ctx) -> None:
    await cb_verify_all_ask(update, context, ())


@callback("backup_status")
async def cb_backup_status(update: Update, context: Ctx, data: tuple) -> None:
    await show_backup_status(update, context)


async def show_backup_status(update: Update, context: Ctx) -> None:
    s = svc(context)
    info = await s.bulk.storage()
    latest = info["latest"]
    used_pct = 100 - info["free"] * 100 // max(info["disk_total"], 1)
    lines = [
        "📊 <b>Backup storage</b>", "",
        f"Path: {code(s.settings.backup_path)}",
        f"Backups: {info['total']} · " + " · ".join(f"{STATUS_EMOJI.get(k, '•')} {k} {v}" for k, v in sorted(info["by_status"].items())),
        f"Space used by backups: {fmt_bytes(info['size'])}",
        f"Disk: {fmt_bytes(info['free'])} free of {fmt_bytes(info['disk_total'])} ({used_pct}% used)",
    ]
    if info["free"] < 5 * 1024**3:
        lines.append("⚠️ Less than 5 GB free: backups before destructive operations may fail (and would stop them).")
    if latest:
        lines.append(f"Latest backup: {STATUS_EMOJI.get(latest.status, '•')} {code(latest.id)} {h(latest.repo)} · {ago(latest.created_at)}")
    lines.append(f"Last automatic run: {ago(info['last_auto'])}")
    await reply(update, context, "\n".join(lines), kb([[btn("⬅️ Backup menu", "backup_menu")]]))


async def cmd_backup_status(update: Update, context: Ctx) -> None:
    await show_backup_status(update, context)


async def cmd_backup_diff(update: Update, context: Ctx) -> None:
    if not context.args:
        raise ValidationError("Usage: /backup_diff BK-YYYYMMDD-NNN")
    await show_backup_diff(update, context, validate_backup_id(context.args[0]))


@callback("bk_diff")
async def cb_backup_diff(update: Update, context: Ctx, data: tuple) -> None:
    await show_backup_diff(update, context, validate_backup_id(data[1]))


async def show_backup_diff(update: Update, context: Ctx, backup_id: str) -> None:
    progress = await ProgressMessage.create(update, context, f"🔍 Comparing {code(backup_id)} with GitHub (read-only)…")
    try:
        diff = await svc(context).bulk.diff(backup_id)
    except BackupError as exc:
        await progress.finish(context, update, f"❌ {safe_error(exc)}")
        return
    lines = [f"🔍 <b>Backup diff</b> {code(backup_id)} ↔ GitHub {code(diff['repo'])}", ""]
    if diff["identical"]:
        lines.append("🟰 Identical: all branches and tags match the backup.")
    else:
        if diff["changed"]:
            lines.append("<b>Changed</b>")
            for c in diff["changed"][:30]:
                extra = ""
                if "ahead" in c:
                    extra = f" · GitHub ahead {c['ahead']} / behind {c['behind']} ({h(c['status'])})"
                elif c.get("status"):
                    extra = f" · {h(c['status'])}"
                lines.append(f"• {code(c['ref'])} {code(c['backup'])} → {code(c['github'])}{extra}")
        if diff["added"]:
            lines += ["<b>Only on GitHub</b>"] + [f"• {code(r)}" for r in diff["added"][:30]]
        if diff["removed"]:
            lines += ["<b>Only in backup</b>"] + [f"• {code(r)}" for r in diff["removed"][:30]]
    await progress.finish(context, update, join_limited(lines), backup_buttons(backup_id))


async def show_repo_backups(update: Update, context: Ctx, text: str, page: int) -> None:
    repo = repo_ref(context, text)
    s = svc(context)
    if not s.policy.show_private:
        try:
            await s.policy.readable(repo)
        except Exception:  # noqa: BLE001 - deleted repos still have backups; hidden private ones do not show
            if await s.policy.target_state(repo) is not None:
                raise
    per_page = 8
    records, total = await s.db.list_backups(limit=per_page, offset=(page - 1) * per_page, repo=repo.full_name,
                                             public_only=not s.policy.show_private)
    lines = [f"💾 <b>Backups of {h(repo.full_name)}</b> ({total})", ""]
    rows = []
    for r in records:
        lines.append(f"{STATUS_EMOJI.get(r.status, '•')} {code(r.id)} · {ago(r.created_at)} · {h(r.reason or '')} · {fmt_bytes(r.size_bytes)}")
        rows.append([btn(f"{STATUS_EMOJI.get(r.status, '•')} {r.id}", "bk", r.id), btn("🔍 Diff", "bk_diff", r.id)])
    if not records:
        lines.append("No backups for this repository.")
    last = max(1, -(-total // per_page))
    rows.append(pager("backup_repo", page, page < last, repo.full_name, last_page=last))
    await reply(update, context, join_limited(lines), kb(rows))


@callback("backup_repo")
async def cb_backup_repo(update: Update, context: Ctx, data: tuple) -> None:
    await show_repo_backups(update, context, data[1], int(data[2]))


async def cmd_backup_repo(update: Update, context: Ctx) -> None:
    if not context.args:
        raise ValidationError("Usage: /backup_repo repo")
    await show_repo_backups(update, context, context.args[0], 1)


# ---------------------------------------------------------- automatic backups
AUTO_CHOICES = {
    "autobackup_enabled": ("Enabled", [False, True]),
    "autobackup_interval_hours": ("Interval (hours)", [6, 12, 24, 48, 168]),
    "autobackup_scope": ("Scope", ["all", "favorites"]),
    "autobackup_skip_unchanged": ("Skip unchanged repositories", [True, False]),
    "autobackup_keep": ("Automatic backups kept per repo (0 = all)", [0, 3, 5, 10]),
}


async def show_autobackup(update: Update, context: Ctx) -> None:
    s = svc(context)
    cfg = await s.bulk.settings()
    lines = ["🤖 <b>Automatic backups</b>", ""]
    rows = []
    for key, (label, _) in AUTO_CHOICES.items():
        value = cfg[key]
        shown = ("yes" if value else "no") if isinstance(value, bool) else value
        lines.append(f"{h(label)}: <b>{h(shown)}</b>")
        rows.append([btn(f"🔁 {label}", "autobackup_set", key)])
    lines += ["", f"Last run: {ago(await s.db.get_setting('autobackup_last_run'))}",
              "Only public repositories you own are backed up. Every backup is verified.",
              "Pruning only removes <i>automatic</i> backups, never the newest verified backup of a repository, "
              "manual or pre-operation backups, or backups used by a pending operation."]
    rows.append([btn("▶️ Run now", "autobackup_now"), btn("⬅️ Backup menu", "backup_menu")])
    await reply(update, context, "\n".join(lines), kb(rows))


@callback("autobackup")
async def cb_autobackup(update: Update, context: Ctx, data: tuple) -> None:
    await show_autobackup(update, context)


@callback("autobackup_set")
async def cb_autobackup_set(update: Update, context: Ctx, data: tuple) -> None:
    key = data[1]
    if key not in AUTO_CHOICES:
        return
    s = svc(context)
    choices = AUTO_CHOICES[key][1]
    current = (await s.bulk.settings())[key]
    index = choices.index(current) if current in choices else -1
    await s.db.set_setting(key, choices[(index + 1) % len(choices)])
    await show_autobackup(update, context)


@callback("autobackup_now")
async def cb_autobackup_now(update: Update, context: Ctx, data: tuple) -> None:
    s = svc(context)
    if s.bulk.busy:
        await reply(update, context, "⏳ A bulk backup is already running.")
        return
    progress = await ProgressMessage.create(update, context, "🤖 Running automatic backup now…")
    try:
        result = await s.bulk.run_autobackup()
    except BackupError as exc:
        await progress.finish(context, update, f"❌ {safe_error(exc)}")
        return
    await progress.finish(context, update, join_limited(_bulk_lines("🤖 <b>Automatic backup finished</b>", result)),
                          kb([[btn("🤖 Settings", "autobackup")]]))


async def cmd_autobackup(update: Update, context: Ctx) -> None:
    await show_autobackup(update, context)
