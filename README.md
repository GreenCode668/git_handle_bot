# GitHub Manager Telegram Bot

A private Telegram bot for managing **one** GitHub account: dashboard, repositories, commits,
history cleanup, profile, badges, backups and GitHub Actions.

It is built around two rules:

- **If an operation could damage Git history and the bot cannot prove that a verified backup exists, it does not run.**
- **Only PUBLIC repositories owned by `GITHUB_USERNAME` are ever modified**, even if the token could technically change private ones.

## Features

| Menu | What it does |
|---|---|
| 📊 Dashboard | Account info, recently pushed repos with last commit, branch count and latest Actions run, recent bot operations |
| 📁 Repositories | List (favorites first), search, recent, create, import, card with rename/archive/delete, ➕ More: stats, languages, tags, releases, issues, PRs, contributors, history stats, description, homepage, topics, favorite, protect |
| 🔀 Commits | Branches, commits, commit details, date filters, compare, `/analyze` with safe history cleanup |
| 👤 Profile | Name, bio, company, location, website, email, X/Twitter, hireable, social links, status |
| 🏅 Badges | Profile README via `/aboutme` (create the repo, template, copy or your own text, preview, revert); badges managed in a marked section only |
| 🏆 PR Automation | Prepare a real change (docs, version bump, formatting, file content), open a pull request, watch checks and reviews, merge when GitHub allows it |
| 💾 Backups | Create, list, details, verify, diff, restore, delete, backup-all, verify-all, storage status, automatic backups |
| ⚙️ Settings | Settings plus monitoring shortcuts: health, rate limit, pending, failed ops, locks, protected, activity, notifications |

### Commands

| Area | Commands |
|---|---|
| Overview | `/start /help /dashboard /status /history /settings` |
| Profile year tabs | `/years /contributions` |
| Security | `/password /lockbot` |
| Code review | `/review repo [commits] [--force]` `/review-issue repo 123` `/review-status` |
| PR automation | `/pr /pr-create /pr-auto /pr-list /pr-status /pr-settings /pr-history /achievements` |
| Accounts | `/accounts /switch name /identity` |
| Repositories | `/repos /recent /search text /repo name /stats /languages /clone /branches /tags /releases /issues /pulls /create name /import url /description repo [text] /homepage repo [url] /topics repo` |
| Favorites | `/favorite repo /unfavorite repo /favorites` |
| Authorship | `/reauthor repo` — map commit author identities to your own (backup + `REAUTHOR owner/repo`) |
| Commits | `/commits [repo] [branch] /latest repo /commit repo sha /commits-before repo date /commits-after repo date /compare repo base head /contributors repo /history-stats repo /analyze repo date /analyze-many [date]` |
| Backups | `/backup [repo] /backups [id] /backup-repo repo /backup-diff id /backup-all /verify-all /backup-status /autobackup /restore id [repo] /undo` |
| Actions | `/actions repo /workflows repo /runs repo /failed repo /rerun repo run-id /cancel-run repo run-id` |
| Profile | `/profile /bio text /name text /location text /website url /socials /social-add [url] /social-remove /aboutme /badges` |
| Monitoring | `/health /ratelimit /failed-ops /locks /activity /notifications` |
| Safety | `/dryrun operation /pending /operation id /cancel [id] /lock repo [reason] /unlock repo /protected` |

Telegram command names cannot contain hyphens, so every hyphenated command is registered with underscores
(`/backup_all`). Typing the hyphenated form also works: the bot dispatches it explicitly, so `/backup-all`
never runs `/backup`.

Quick actions: `repo @info | @stats | @commits | @branches | @actions | @backup | @rename new-name | @archive | @unarchive | @remove`.

## Automated code review and fix

`/review my-repo 20` clones the repository shallowly into `TEMP_DIR`, reads the recent commits and diffs,
detects the project type, **reads GitHub's own check results for those commits** (failed workflows, check-run
annotations with file and line, failed commit statuses, plus the error lines from failed job logs), runs the
project's own linter and tests, scans the diff for committed secrets,
`.env` files and common defects (bare `except`, shell-enabled subprocess calls, `eval`, unhandled promises),
and reports findings grouped by severity. `/review-issue my-repo 123` starts from an open issue instead.

If something is fixable, a **🛠 Fix** button appears. Claude Code (`CLAUDE_CODE_PATH`, headless
`claude -p … --output-format json`) edits the clone, capped by `REVIEW_TIMEOUT_MS` and `MAX_FILES_PER_FIX`.
The linter and tests are then re-run; if they fail, the branch is discarded and no pull request is opened.
Otherwise you see the diffstat and the first 40 diff lines and nothing is pushed until you tap **Approve**,
which pushes `bot/fix-<sha>-<timestamp>` and opens a **draft** pull request listing every finding.

The default branch is never committed to, nothing is force-pushed, and these pull requests are never merged
by the bot. Repositories you do not own require `--force` and are reviewed read-only: no linters, no tests and
no fixes, because running a project's scripts runs its code. Everything sent to Telegram or into the model
prompt passes through the secret redactor, and the clone is deleted in a `finally` block.

Requires the Claude Code CLI on the server: `curl -fsSL https://claude.ai/install.sh | bash`, plus credentials
(`ANTHROPIC_API_KEY` or `claude setup-token`) in the bot's environment. `/review-status` shows whether it is found.

## Pull request automation

`/pr` opens the menu; `/pr-create` walks through repository → change type → change → commit message → diff and
impact → confirmation. The bot then creates a branch, commits, opens the pull request and records it in the
operation history. `/pr-status` shows check runs, commit statuses and reviews; merging needs the typed phrase
`MERGE owner/repo`, or happens by itself when auto-merge is on.

Change types: documentation/README text, an exact text replacement (version bumps), whitespace cleanup, and
full file content you provide. A change that would not alter the file is refused, so no empty commits.

Settings per account (`/pr-settings`): auto merge (default OFF), merge method (squash/merge/rebase), delete
branch after merge, require successful checks, allowed repositories, maximum concurrent PR operations.

**Never:** bypass branch protection, bypass or fake reviews, force-push a protected branch, or touch a private
repository. Merges go through GitHub's merge API, so GitHub enforces its own rules; the bot additionally refuses
to merge while a required check is failing, pending or has not reported yet.

`/achievements` shows real counts only (pull requests opened, merged and reviewed, commits, issues). GitHub has
no API for achievement progress, so nothing there is predicted or promised.

## Safety model

Destructive operations (delete repository, history rewrite, restore/force push) always follow:

```
Analyze → show impact → you approve → create mirror backup → verify backup
        → you type the phrase (DELETE owner/repo, REWRITE owner/repo, RESTORE owner/repo)
        → re-verify backup + check GitHub still matches it → execute → verify result → report
```

- Backup creation or verification fails → **stop**. GitHub is not touched.
- GitHub changed after the backup was taken → **stop**.
- Pushes are `--atomic` with `--force-with-lease` pinned to the exact SHAs in the verified backup,
  so a concurrent push makes the whole push fail with no partial changes.
- One destructive operation per repository at a time (SQLite lock).
- Confirmations expire (default 15 minutes). Operations interrupted by a restart are reported on startup.
- Making a repository public requires typing `PUBLIC owner/repo`. Rename, private and archive need a button confirmation.

### Public-only policy

A central `RepoPolicy` is enforced by the safety engine for **every** GitHub write: when an operation
starts, when you approve it, and again immediately before it executes, each time using fresh repository
metadata from GitHub. A repository must be owned by `GITHUB_USERNAME`, have visibility `public`,
and not be manually protected (`/lock`). Consequences:

- `@public` on a private repository is always refused (it would modify a private repository).
- `@private` is allowed on a public repository, but afterwards the bot can no longer manage it, including undo.
- Create, import and restore only ever create public repositories.
- Backups (manual, bulk, automatic) are only taken of managed public repositories.
- Read-only views hide private repositories completely unless `SHOW_PRIVATE_REPOS=true`.
  Even then they are shown read-only.

### Manual protection, dry runs and pending operations

- `/lock repo [reason]` blocks all write and destructive operations on a repository (stored in SQLite, survives restarts).
  `/unlock` goes through the confirmation flow.
- `/dryrun delete repo`, `/dryrun rewrite repo 2025-01-01`, `/dryrun restore BK-… [repo]`, `/dryrun repo @remove` and similar
  run the policy, lock and analysis checks and describe the backup and confirmation steps, without locking, backing up or changing anything.
- `/pending`, `/operation id` and `/cancel id` let you inspect and cancel operations waiting for you.

### Backups (`BK-YYYYMMDD-NNN`)

Each backup directory under `BACKUP_PATH` contains:

- `repo.git`: a `git clone --mirror` with every ref (branches, tags, `refs/pull/*`, notes) and all history
- `repo.bundle`: a portable bundle of all branches and tags
- `manifest.json`: every ref and SHA, commit count, bundle SHA-256, HEAD, warnings
- `metadata.json`: visibility, description, topics, default branch and similar (used to recreate a deleted repo)
- `wiki.git`: the wiki, if it exists and wiki backups are enabled

Verification runs `git fsck --full`, compares refs with the manifest, checks the commit count and
bundle checksum, then restores the bundle into a scratch repository and compares every branch and tag.

**Bulk and automatic backups:** `/backup-all` asks first, then backs up every managed public repository
(optionally only changed ones). `/autobackup` configures interval, scope (all or favorites), skip-unchanged
and retention. Retention only prunes *automatic* backups. It never removes the newest verified backup of a
repository, manual or pre-operation backups, or a backup used by a pending operation. `/verify-all` reports
damaged and missing backups. `/backup-diff` compares a backup with GitHub.

**Not included** (GitHub does not store these in git): issues, PR discussions, release assets,
Actions secrets and logs, stars, LFS objects. The bot says so before deleting anything.

### Safe history cleanup

`/analyze my-repo 2025-01-01` clones a read-only copy and reports total commits, commits before and
after the cutoff, affected branches and tags, merges crossing the cutoff, date anomalies, signed
commits and whether a rewrite is needed. Nothing is modified.

> **Note on profile year tabs:** rewriting history removes commits, but GitHub keeps the contributions it
> recorded when they were pushed, and counts the rewritten commits again in the years you keep. Only deleting
> a repository (or making it private) removes its contributions. `/contributions` shows which repositories hold
> which years and offers a bulk deletion with one verified backup and one `DELETE owner/repo` phrase per repository.

`/analyze-many [date]` (or **🔬 Analyze old commits in many repositories** in the repository lists) runs the
same analysis for **all** public repositories or a **selected** set, then shows one report. You tick the repositories
to clean. A repository where *nothing* would survive the cutoff (every commit is older) is offered as a
**repository deletion** instead, because a rewrite would only leave snapshot commits behind; it then needs
`DELETE owner/repo` instead of `REWRITE owner/repo`, and its backup covers only git data, not issues or releases. The bot creates and verifies a backup for each ticked repository and gives you one `REWRITE owner/repo`
phrase per repository. Nothing is rewritten until you send that repository's phrase, and repositories are never deleted.

**Removal style.** The bot removes pre-cutoff commits **without a summary commit**: the first commits after the
cutoff become the new starting commits and already contain every older file. A branch or kept tag with *no*
commit after the cutoff keeps one "Snapshot of history before …" commit, because its files would otherwise be
lost. (The engine also supports a single summary-commit style; see `DEFAULT_SQUASH_STYLE` in `ghbot/services/operations.py`.)

The rewrite is **tree-preserving**:

1. A commit is "old" only if it is dated before the cutoff **and** all its parents are old, so no newer commit can be orphaned.
2. Old parents are dropped from the first newer commits (no summary commit). Old branch tips, and old tag targets if kept, become parentless snapshot commits with exactly the same files.
3. Newer commits are re-created byte-for-byte (author, committer, dates, message) with only parent pointers changed.
4. Before pushing: `git fsck`, every branch tip's tree hash must equal the original, no old commit may remain reachable, and the commit count must match.
5. Atomic force-with-lease push, then GitHub's refs are compared with the verified local result.

Old tags can be kept as snapshot commits (default) or deleted. Undo with `/undo`.
Old SHAs may stay cached on GitHub (forks, PR refs) until GitHub garbage-collects them.
Contact GitHub Support if you are removing leaked secrets.

## Setup

### Several GitHub accounts

Add more accounts to `.env` and restart:

```
GITHUB_USERNAME_2=second-account
GITHUB_TOKEN_2=ghp_second_token
GITHUB_EMAIL_2=second@example.com      # optional commit identity for that account
GITHUB_NAME_2=Second Name              # optional; defaults to the username
```

(continue with `_3`, `_4` … up to `_10`). Tokens stay in the file and never pass through Telegram.
Each account keeps its own commit identity (used by `/reauthor`): `GITHUB_EMAIL`/`GITHUB_NAME` in `.env`,
or `/identity` in the bot, which overrides the file for that account only.
`/accounts` lists them with their identities, `/switch name` changes the active one. One account is active at a time and every
command applies to it. Switching is refused while operations are pending, so a confirmation can never land on
the wrong account. Backups keep the owner in their name, and settings such as the commit identity are per account.

### 1. Tokens

- **Telegram:** create a bot with [@BotFather](https://t.me/BotFather). Get your numeric user id from e.g. @userinfobot.
- **GitHub:** a classic personal access token with scopes `repo`, `delete_repo`, `user` and `workflow` is recommended.
  Fine-grained tokens also work for most features (Administration, Contents, Actions and Workflows set to read/write
  on all repositories, plus account Profile write). The user-status GraphQL mutation may need a classic token.

### 2. Run locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in values
.venv/bin/python -m ghbot
```

Requires Python 3.11+ and `git` 2.30+ on PATH. Send `/start` to your bot and set a password when asked.

### 3. Docker

```bash
cp .env.example .env   # fill in values
mkdir -p data && sudo chown 10001:10001 data
docker compose up -d --build
```

Data (database, backups, work directories) lives in `./data`.

## Security

### Password lock

The bot starts **locked**. Until the correct password arrives, no command, button or text is processed.

- On first start it asks you to set a password (at least 8 characters, entered twice).
- Only a scrypt hash (32 MB cost) and a random salt are stored, in the SQLite database. The password is never logged.
- The message containing the password is deleted from the chat immediately.
- After 5 wrong attempts the bot refuses all attempts for 15 minutes, even correct ones.
- `/password` changes it (current password required), `/lockbot` locks immediately, and ⚙️ Settings has an
  auto-lock timeout (off, 15, 60 or 240 minutes of inactivity).
- Scheduled automatic backups still run while locked, but their details are only shown after unlocking.

Forgot the password? Stop the bot and run
`sqlite3 data/bot.db "DELETE FROM auth;"` on the server, then set a new one at the next start.

- Only `TELEGRAM_ALLOWED_USER_ID` in a private chat is served. Everything else is dropped before any handler runs.
- Tokens are never logged. Every log handler applies redaction, httpx URL logging is silenced, and git stderr is redacted.
- Git runs without a shell and with list arguments. System and global git config and credential helpers are disabled,
  and prompts are off. The GitHub token is passed as an in-memory HTTP header scoped to `https://github.com/`:
  it never appears in URLs, command lines or files on disk.
- Only `https` transports are allowed for user-supplied URLs (no `file://`, `ssh`, `ext::`). URLs with credentials,
  IP literals or local hostnames are rejected.
- Repository names, branch names, dates, backup IDs, profile values and badge text are all validated.
- The bot only modifies public repositories owned by `GITHUB_USERNAME` (see the public-only policy).
- `/notifications` needs a classic token (`notifications` or `repo` scope); fine-grained tokens cannot read notifications.

## Development

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
.venv/bin/ruff check ghbot tests
```

The tests use real git repositories (branches, merges, annotated tags, date skew) and a fake GitHub API.
They cover backup create/verify/tamper detection, every stop condition of the safety flow, lease rejection
on concurrent pushes, rewrite → undo, restoring a deleted repository, redaction and input validation.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design.
