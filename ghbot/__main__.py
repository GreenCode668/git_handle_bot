"""Entry point: python -m ghbot"""

from __future__ import annotations

import logging
import sys

from telegram import Update

from ghbot.config import ConfigError, load_settings
from ghbot.logging_setup import setup_logging


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    setup_logging(settings.log_level, settings.secrets)
    from ghbot.bot.app import build_application

    app = build_application(settings)
    logging.getLogger(__name__).info("Starting bot for GitHub user %s", settings.github_username)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
