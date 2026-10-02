GitHub Manager Telegram Bot
A private Telegram bot for managing one GitHub account: dashboard, repositories, commits, history cleanup, profile, badges, backups and GitHub Actions.

It is built around two rules:

If an operation could damage Git history and the bot cannot prove that a verified backup exists, it does not run.
Only PUBLIC repositories owned by GITHUB_USERNAME are ever modified, even if the token could technically change private ones.
