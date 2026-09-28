"""In-bot guide: what every action does and exactly how to run it."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Topic:
    key: str
    title: str
    lines: list[str]
    # (button label, callback route, *args) - routes must exist in the callback registry
    actions: list[tuple] = field(default_factory=list)


SAFETY_LINE = ("Every change follows: analyze → impact → you approve → backup → verify → you type the phrase → "
               "execute → verify → report. Nothing changes before the typed phrase.")

TOPICS: list[Topic] = [
    Topic("start", "🚀 Getting started", [
        "<b>1.</b> Unlock: after a restart the bot asks for your password.",
        "<b>2.</b> Use the menu buttons at the bottom, or type commands.",
        "<b>3.</b> Anything that changes GitHub asks first and shows what would happen.",
        "",
        "<b>Rules that never change</b>",
        "• Only your Telegram account can use this bot.",
        "• Only <b>public</b> repositories you own are ever modified.",
        "• Destructive steps need a verified backup first.",
        "",
        SAFETY_LINE,
        "",
        "<b>Useful anytime</b>",
        "/cancel stops what is pending · /pending shows waiting confirmations · /status and /health check the bot.",
    ], [("📁 Repositories", "repos", "detail", 1), ("🩺 Health", "health")]),

    Topic("repos", "📁 Repositories", [
        "<b>See them</b>: 📁 Repositories, /repos, /recent (recently updated), /search text, /favorites.",
        "<b>One repository</b>: tap it, or /repo name. The card shows visibility, branches, last commit and Actions.",
        "<b>➕ More</b> on the card: stats, languages, tags, releases, issues, pull requests, contributors, "
        "description, homepage, topics, clone URL, backups, favorite, protect.",
        "",
        "<b>Quick text actions</b> (type them in the chat):",
        "<code>repo @info</code> · <code>@stats</code> · <code>@commits</code> · <code>@branches</code> · "
        "<code>@actions</code> · <code>@backup</code>",
        "<code>repo @rename new-name</code> · <code>@archive</code> · <code>@unarchive</code> · <code>@remove</code>",
        "",
        "<b>Create / import</b>: /create name makes a public repository · /import url copies another git repository "
        "with its history, branches and tags.",
        "",
        "Renaming, archiving and visibility need one confirmation tap. Deleting needs the typed phrase "
        "<code>DELETE owner/repo</code> after a verified backup.",
    ], [("📁 Open", "repos", "detail", 1), ("⭐ Favorites", "favorites")]),

    Topic("commits", "🔀 Commits & history", [
        "<b>Browse</b>: /commits repo [branch] · /latest repo · /branches repo · /tags repo",
        "<b>One commit</b>: /commit repo sha — author, dates, message, files, signature.",
        "<b>By date</b>: /commits-before repo 2020-01-01 · /commits-after repo 2020-01-01",
        "<b>Compare</b>: /compare repo main v1.0 · <b>People</b>: /contributors repo · <b>Range</b>: /history-stats repo",
        "",
        "<b>Remove old commits (one repository)</b>",
        "<code>/analyze my-repo 2020-01-01</code> → read-only report → 💾 Continue: backup → type "
        "<code>REWRITE owner/repo</code>.",
        "Commits before the date are removed; the first newer commit becomes the start and keeps every older file, "
        "so your current files never change.",
        "",
        "<b>Many repositories</b>: /analyze-many [date] — pick all or some, one report, then one backup and one "
        "phrase per repository.",
        "",
        "⚠️ Rewriting changes every commit id: anyone with a clone must re-clone. /undo restores the backup.",
    ], [("📊 Contributions", "contribs"), ("📅 Commit years", "years")]),

    Topic("years", "📊 Profile year tabs", [
        "The year list next to your contribution graph is not a setting. A year appears when GitHub counted "
        "contributions for it.",
        "",
        "<b>/contributions</b> shows, per year, which repositories caused it.",
        "<b>/years</b> shows the first and last commit year per repository.",
        "",
        "⚠️ <b>Rewriting history does not remove a year.</b> GitHub keeps the contributions recorded when the "
        "commits were pushed, and counts the rewritten commits again in the years you keep.",
        "",
        "<b>What does work</b>",
        "• Delete the repository: its contributions go with it (/contributions → 🗑 Delete those repositories).",
        "• Or make it private on github.com: its contributions stop being shown publicly.",
        "",
        "A year with 0 contributions cannot be removed at all; the tab is simply empty.",
    ], [("📊 Contributions", "contribs"), ("📅 Years", "years")]),

    Topic("authors", "✍️ Commit authors", [
        "<b>/reauthor repo</b> lists every author identity with a commit count.",
        "",
        "<b>1.</b> Tick the identities that are <b>yours</b>.",
        "<b>2.</b> Set the identity commits should get (Name &lt;email&gt;), saved for next time.",
        "<b>3.</b> 💾 Continue: backup, then type <code>REAUTHOR owner/repo</code>.",
        "",
        "Dates, messages and files stay exactly the same; only author and committer name/email change.",
        "Every commit id changes (force push), and GitHub counts those commits as contributions again.",
        "",
        "⚠️ Only remap identities that are yours. Licences normally require keeping other people's attribution.",
    ]),

    Topic("backups", "💾 Backups & restore", [
        "A backup is a full mirror: every branch, tag, ref and commit, plus a bundle and a manifest.",
        "It is verified with git fsck, a ref comparison and a restore test before it counts as valid.",
        "",
        "<b>Make one</b>: /backup repo · <b>All public repos</b>: /backup-all · <b>Automatic</b>: /autobackup",
        "<b>Look</b>: /backups · /backup-repo repo · /backup-status · <b>Compare with GitHub</b>: /backup-diff BK-id",
        "<b>Check</b>: /verify-all · <b>Bring back</b>: /restore BK-id [repo] · /undo (last destructive operation)",
        "",
        "Restoring recreates the repository if it is gone, with all code, branches and tags.",
        "❗ Not in a git backup: issues, pull request discussions, releases, stars and Actions secrets.",
        "",
        "Before every destructive operation a fresh backup is made and verified; a recent one is reused when "
        "GitHub has not changed since.",
    ], [("💾 Backups", "backup_menu"), ("🤖 Automatic", "autobackup")]),

    Topic("actions", "⚙️ GitHub Actions", [
        "<b>/actions repo</b> — workflows and recent runs, filter by running, queued, success or failed.",
        "<b>/workflows repo</b> · <b>/runs repo</b> · <b>/failed repo</b>",
        "",
        "Open a run to re-run it, re-run only failed jobs, or cancel it while it is running.",
        "<b>/rerun repo run-id</b> · <b>/cancel-run repo run-id</b>",
        "",
        "Each one asks for confirmation and then checks GitHub really did it.",
    ], [("📁 Pick a repository", "repos", "actions", 1)]),

    Topic("profile", "👤 Profile & badges", [
        "<b>/profile</b> shows and edits: name, bio, company, location, website, public email, X/Twitter, "
        "available-for-hire, social links and your status.",
        "Shortcuts: /bio text · /name text · /location text · /website url · /socials · /social-add · /social-remove",
        "Every change shows old → new and asks to confirm, then verifies it on GitHub.",
        "",
        "<b>/aboutme</b> manages the profile README GitHub shows on your profile page. It creates the repository "
        "named like your account if needed, then fills README.md from a template, from another repository's README, "
        "or from text you send. You see a preview, confirm, and ↩️ Revert restores the previous version.",
        "",
        "<b>/badges</b> manages badges inside a marked section of your profile README "
        "(username/username repository).",
        "Technology badges, Shields.io badges, stats cards, followers, stars, profile views.",
        "Content outside the marked section is never touched, and the previous README is saved so you can revert.",
    ], [("👤 Profile", "prof_show"), ("🪪 Profile README", "aboutme"), ("🏅 Badges", "badges")]),

    Topic("prs", "🏆 PR automation", [
        "For real maintenance changes: documentation, a version bump, a formatting cleanup, or file content "
        "you provide. The bot does not invent commits.",
        "",
        "<b>/pr</b> opens the menu · <b>/pr-create [repo]</b> starts one.",
        "<b>1.</b> Pick the repository · <b>2.</b> Pick the change type · <b>3.</b> Send the change · "
        "<b>4.</b> Send a commit message (or use the default).",
        "<b>5.</b> Check the diff and impact, then confirm. The bot creates a branch, commits, and opens the PR.",
        "",
        "<b>/pr-status [id]</b> shows checks, reviews and whether it can merge · <b>/pr-list</b> open ones · "
        "<b>/pr-history</b> everything.",
        "Merging needs the typed phrase <code>MERGE owner/repo</code>, unless auto-merge is on.",
        "",
        "<b>/pr-settings</b>: auto-merge, merge method (squash/merge/rebase), delete branch after merge, "
        "require successful checks, allowed repositories, maximum concurrent operations.",
        "<b>/pr-auto on|off</b> toggles auto-merge (default OFF). With it on, a confirmed pull request merges "
        "by itself once every required check and review passes.",
        "",
        "The bot never bypasses branch protection, never approves its own pull requests, and never force-pushes. "
        "Private repositories are refused, as everywhere else.",
        "",
        "<b>/achievements</b> shows real counts (PRs opened, merged, reviewed, commits, issues). GitHub has no "
        "API for achievement progress, so nothing there is a prediction.",
    ], [("🏆 PR automation", "pr_menu"), ("🏅 Statistics", "achievements")]),

    Topic("review", "🔍 Code review & fix", [
        "<b>/review repo [commits]</b> clones the repository shallowly, reads the recent commits and diffs, "
        "detects the project type, reads GitHub's own check results for those commits (the red ✗ on github.com), "
        "runs its linter and tests, scans for committed secrets and common defects, and reports findings "
        "by severity.",
        "<b>/review-issue repo 123</b> does the same starting from an open issue.",
        "",
        "If something is fixable, a <b>🛠 Fix</b> button appears. Claude Code then edits the clone, the linter and "
        "tests are run again, and you see the diffstat and the first lines of the diff.",
        "<b>Nothing is pushed before you tap Approve.</b> Approving pushes a new branch "
        "<code>bot/fix-…</code> and opens a <b>draft</b> pull request listing every finding.",
        "If the checks fail after the fix, the branch is discarded and no pull request is opened.",
        "",
        "The bot never commits to the default branch, never force-pushes and never merges these pull requests.",
        "Repositories you do not own need <code>--force</code> and are reviewed read-only: no linters, no tests, "
        "no fixes, because running a project's scripts runs its code.",
        "",
        "<b>/review-status</b> shows whether the Claude Code CLI is installed and the current limits "
        "(REVIEW_TIMEOUT_MS, MAX_FILES_PER_FIX, TEMP_DIR).",
    ], [("🔍 Status", "guide", "review")]),

    Topic("safety", "🛡 Safety & confirmations", [
        SAFETY_LINE,
        "",
        "<b>Phrases</b>: <code>DELETE owner/repo</code>, <code>REWRITE owner/repo</code>, "
        "<code>RESTORE owner/repo</code>, <code>REAUTHOR owner/repo</code>, <code>PUBLIC owner/repo</code>.",
        "Type them exactly, as a new message. Editing an old message never counts.",
        "",
        "<b>/dryrun …</b> simulates without locking, backing up or changing anything.",
        "<b>/pending</b> lists what waits for you · <b>/operation id</b> shows one in full · "
        "<b>/cancel id</b> cancels it · <b>/cancel</b> cancels everything pending.",
        "<b>/lock repo</b> blocks all writes to a repository · <b>/unlock repo</b> removes it · <b>/protected</b> lists them.",
        "",
        "One repository can only have one operation at a time. If it says 'locked by another operation', "
        "either send that operation's phrase or cancel it in /pending.",
        "Confirmations expire (⚙️ Settings sets the timeout). An expired one changes nothing; just start again.",
    ], [("⏳ Pending", "pending"), ("🛡 Protected", "protected")]),

    Topic("security", "🔒 Password & security", [
        "The bot starts locked. Until the password arrives, no command, button or message is processed.",
        "",
        "<b>/password</b> changes it (current password first, then the new one twice).",
        "<b>/lockbot</b> locks immediately.",
        "⚙️ Settings has an auto-lock timeout after inactivity.",
        "",
        "Only a scrypt hash is stored, never the password itself, and your password message is deleted from the chat.",
        "After 5 wrong attempts everything is refused for 15 minutes.",
        "",
        "Your GitHub and Telegram tokens are never shown or logged.",
    ], [("🔐 Change password", "password")]),

    Topic("monitor", "🩺 Status & monitoring", [
        "<b>/health</b> — GitHub API, Telegram, database, git, backup storage and free disk.",
        "<b>/status</b> — token scopes, rate limit, locks and pending operations.",
        "<b>/ratelimit</b> — remaining GitHub API calls · <b>/locks</b> — locked repositories.",
        "<b>/history</b> — everything the bot has done · <b>/failed-ops</b> — what failed and why.",
        "<b>/activity</b> — recent GitHub activity · <b>/notifications</b> — unread notifications "
        "(classic tokens only).",
    ], [("🩺 Health", "health"), ("🧾 History", "history", 1)]),

    Topic("trouble", "🆘 When something looks wrong", [
        "<b>'locked by another operation'</b> — an earlier operation still waits. /pending → send its phrase or cancel it.",
        "<b>Confirmation expired</b> — nothing changed. Start again; existing backups are reused, so it is quick.",
        "<b>'not found or is not managed'</b> — the name is wrong, the repository is private, or your fine-grained "
        "token does not include it (GitHub → Settings → Developer settings → token → Repository access).",
        "<b>Buttons do nothing after a restart</b> — old buttons expire. Reopen the menu.",
        "<b>Date rejected</b> — use YYYY-MM-DD, for example 2020-01-01, in a new message.",
        "<b>Year tabs still there</b> — see 📊 Profile year tabs: only deleting or hiding a repository removes them.",
        "<b>Something went wrong mid-operation</b> — /failed-ops shows it, with the backup id to /restore.",
    ], [("⏳ Pending", "pending"), ("❌ Failed operations", "failed_ops", 1)]),
]

BY_KEY = {topic.key: topic for topic in TOPICS}
