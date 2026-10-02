GitHub Manager Telegram Bot
A private Telegram bot for managing one GitHub account: dashboard, repositories, commits, history cleanup, profile, badges, backups and GitHub Actions.

It is built around two rules:

If an operation could damage Git history and the bot cannot prove that a verified backup exists, it does not run.
Only PUBLIC repositories owned by GITHUB_USERNAME are ever modified, even if the token could technically change private ones.


Automated code review and fix
/review my-repo 20 clones the repository shallowly into TEMP_DIR, reads the recent commits and diffs, detects the project type, reads GitHub's own check results for those commits (failed workflows, check-run annotations with file and line, failed commit statuses, plus the error lines from failed job logs), runs the project's own linter and tests, scans the diff for committed secrets, .env files and common defects (bare except, shell-enabled subprocess calls, eval, unhandled promises), and reports findings grouped by severity. /review-issue my-repo 123 starts from an open issue instead.

If something is fixable, a 🛠 Fix button appears. Claude Code (CLAUDE_CODE_PATH, headless claude -p … --output-format json) edits the clone, capped by REVIEW_TIMEOUT_MS and MAX_FILES_PER_FIX. The linter and tests are then re-run; if they fail, the branch is discarded and no pull request is opened. Otherwise you see the diffstat and the first 40 diff lines and nothing is pushed until you tap Approve, which pushes bot/fix-<sha>-<timestamp> and opens a draft pull request listing every finding.

The default branch is never committed to, nothing is force-pushed, and these pull requests are never merged by the bot. Repositories you do not own require --force and are reviewed read-only: no linters, no tests and no fixes, because running a project's scripts runs its code. Everything sent to Telegram or into the model prompt passes through the secret redactor, and the clone is deleted in a finally block.

Requires the Claude Code CLI on the server: curl -fsSL https://claude.ai/install.sh | bash, plus credentials (ANTHROPIC_API_KEY or claude setup-token) in the bot's environment. /review-status shows whether it is found.

Pull request automation
/pr opens the menu; /pr-create walks through repository → change type → change → commit message → diff and impact → confirmation. The bot then creates a branch, commits, opens the pull request and records it in the operation history. /pr-status shows check runs, commit statuses and reviews; merging needs the typed phrase MERGE owner/repo, or happens by itself when auto-merge is on.



