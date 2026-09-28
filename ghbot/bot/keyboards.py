"""Main reply keyboard."""

from __future__ import annotations

from telegram import ReplyKeyboardMarkup

DASHBOARD = "📊 Dashboard"
REPOSITORIES = "📁 Repositories"
COMMITS = "🔀 Commits"
PROFILE = "👤 Profile"
BADGES = "🏅 Badges"
BACKUPS = "💾 Backups"
PRS = "🏆 PR Automation"
SETTINGS = "⚙️ Settings"

MENU_LABELS = (DASHBOARD, REPOSITORIES, COMMITS, PROFILE, BADGES, BACKUPS, PRS, SETTINGS)


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[DASHBOARD, REPOSITORIES], [COMMITS, PROFILE], [BADGES, BACKUPS], [PRS, SETTINGS]],
        resize_keyboard=True,
        is_persistent=True,
    )
