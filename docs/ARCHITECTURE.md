# Architecture

## Layers

```
ghbot/
├── __main__.py            entry point (python -m ghbot)
├── config.py              env loading and validation (.env via python-dotenv)
├── logging_setup.py       Redactor + RedactingFilter on every log handler
├── validators.py          all user-input validation (repo, owner, branch, date, URL, backup id, @actions)
├── db.py                  SQLite (WAL): operations, backups, repo_locks, settings, badges, readme_snapshots
├── github/
│   ├── client.py          async httpx client: retries (GET/5xx), Link pagination, rate-limit and scope tracking
│   └── api.py             typed endpoint wrappers (repos, contents, actions, user, social accounts, GraphQL status)
├── git/
│   ├── runner.py          safe subprocess wrapper (no shell, isolated config, scoped auth header, redaction)
│   ├── mirror.py          ls-remote, mirror clone, bundle, backup artifacts, verification
│   ├── history.py         read-only analysis, tree-preserving cutoff squash, author rewrite, push arguments
│   └── stats.py           blobless clone + commit-history statistics
├── services/
│   ├── container.py       Services: one AccountResources (client, git runner, backups, policy) per
│   │                      configured account, with the active one exposed as .gh/.git/.backups/.policy
│   ├── backups.py         backup lifecycle and GitHub-vs-backup comparison
│   ├── auth.py            PasswordLock: scrypt hash, unlock/lockout, auto-lock
│   ├── policy.py          RepoPolicy: owned + public + not protected (writes); visibility filter (reads)
│   ├── safety.py          SafetyEngine state machine + OperationSpec base class + dry runs
│   ├── operations.py      delete, rewrite, restore, rename, visibility, archive, import, create,
│   │                      description, homepage, topics, Actions rerun/cancel, unlock
│   ├── bulk.py            backup-all, automatic backups + retention, verify-all, backup diff, storage
│   ├── review.py          shallow clone, linters/tests, secret+defect scan, Claude Code fix, draft PR
│   ├── pullrequests.py    change builders, branch+commit+PR creation, checks/review status, merge rules
│   ├── profile.py         profile field specs and normalization
│   └── badges.py          badge rendering and marker-section merge
└── bot/
    ├── app.py             Application wiring, command list, jobs, startup recovery
    ├── router.py          callback registry and pending text-input registry
    ├── ui.py              HTML escaping, pagination, progress messages
    ├── keyboards.py       main reply keyboard
    └── handlers/          common, dashboard, repos, repoinfo, commits, actions, backups, profile, badges,
                           monitoring, safetycmds, operations
```

Handlers never call git directly. Everything that modifies GitHub repositories goes through `SafetyEngine`.

## Telegram menus

```
Main keyboard: 📊 Dashboard | 📁 Repositories | 🔀 Commits | 👤 Profile | 🏅 Badges | 💾 Backups | ⚙️ Settings

📁 Repositories → paginated list → repository card
    [🌿 Branches] [🔀 Commits] [⚙️ Actions] [💾 Backup now]
    [✏️ Rename] [🔒/🌍 Visibility] [📦 Archive] [🔬 Analyze history] [🗑 Delete]
💾 Backups → [Create] [List] [Details] [Restore] [Verify] [Delete] → paginated backup picker
🏅 Badges → [+Technology] [+Custom] [+Stats/Top langs/Streak/Followers/Stars/Views] [Reorder/Remove] [Preview] [Publish] [Revert]
👤 Profile → [✏️ each field] [+/- social link] [set/clear status]
```

Callback data uses PTB's arbitrary callback data (tuples like `("pick", "detail", "me/repo")`), so
repository names never have to fit in 64 bytes. Buttons that expired after a restart get a friendly alert.

## Database

| table | purpose |
|---|---|
| `operations` | every bot action. Safety-engine operations carry `stage`, `params`, `impact`, `result`, `backup_id`, `confirm_phrase`, `expires_at` |
| `backups` | backup id, repo, path, status (`creating`/`created`/`verified`/`failed`/`deleted`), counts, checksum, timestamps |
| `backup_counters` | atomic per-day counter for `BK-YYYYMMDD-NNN` (`INSERT … ON CONFLICT … RETURNING`) |
| `repo_locks` | one row per locked repository (primary key = lowercase `owner/repo`) |
| `settings` | JSON key/value user settings |
| `badges` | badge definitions and order |
| `readme_snapshots` | README content before each badge publish (for revert) |
| `favorites` | favorite repositories (shown first in repository menus) |
| `protected_repos` | manual `/lock` protections (persist across restarts, unlike operation locks) |
| `pull_requests` | one row per PR operation: repo, number, branch, base, change type, status, auto-merge, merge method, head sha, operation id (schema v4) |
| `auth` | single row: scrypt algorithm, salt, hash, parameters, failed attempts, lockout deadline (schema v3) |

`backups.visibility` (added in schema v2) records the repository visibility at backup time, so hidden
private-repository backups stay out of listings.

## Handler order

```
group -3  auth_guard   only TELEGRAM_ALLOWED_USER_ID in a private chat survives
group -2  lock_gate    password lock: stops every update until unlocked (setup flow when none is set)
group -1  hyphen_command   '/backup-all' style aliases, so they never fall through to '/backup'
group  0  commands, callback router, text router
```

The session is unlocked in memory only, so a restart always locks the bot again.

## Policy enforcement

`SafetyEngine._policy()` runs for every spec with `github_write = True` at `start()`, `approve()` and
inside `_execute()` right before the backup re-verification. It fetches fresh metadata:

- target exists → must be owned, `private == false`, `visibility == "public"` and not in `protected_repos`
- target missing and the spec `may_create` → the created visibility (`creation_private`) must be public
- `BackupService.create` independently refuses non-public repositories

`UnlockRepository` is the only spec with `github_write = False`, because it only changes the bot's database.

## Safety engine stages

```
start()          → ANALYZED            (impact stored; blockers → CANCELLED)
approve()        → lock → BACKING_UP → backup + verify → AWAITING_CONFIRMATION
                   (backup failure → FAILED, lock released, GitHub untouched)
confirm_phrase() → EXECUTING → re-verify backup → GitHub refs == backup? → preflight → execute → verify → DONE/FAILED
cancel()/expiry  → CANCELLED/EXPIRED (lock released)
startup          → BACKING_UP/EXECUTING → INTERRUPTED, all locks cleared, user notified
```

All transitions are `UPDATE … WHERE id=? AND stage IN (…)` compare-and-set, so double taps are harmless.
Backups are skipped only when the target repository does not exist (restore of a deleted repo, import),
because then nothing exists that could be damaged.

## Backup and restore

- **Create:** `ls-remote` → `clone --mirror` → refs must match the advertisement → bundle → manifest/metadata
  → atomic rename from `.tmp-BK-…` to `BK-…`.
- **Verify:** fsck, refs == manifest, commit count, bundle sha256, bundle restored into a scratch repo with refs compared, fsck of the scratch repo.
- **Restore:** a local clone of the backup (the backup itself is never written to) → create the repo from metadata if missing
  → atomic lease push of creates/updates → set default branch → atomic lease push of deletions → `ls-remote` must equal the backup.
- **Undo:** restore the backup attached to the latest destructive operation (fallback: latest verified backup), through the full safety flow.

## History rewrite

See the module docstring in `ghbot/git/history.py` and the README. Key guarantees:

- Ancestry-closed "old" set, so newer commits are never orphaned.
- The file tree of every branch tip is unchanged (checked by hash).
- No pre-cutoff commit remains reachable, and the commit count is exactly as planned.
- Rewriting happens in a disposable clone of the verified backup, so leases pin to the backed-up SHAs.
- Refspecs carry no `+`, because `+` would bypass `--force-with-lease`. A regression test covers this.
