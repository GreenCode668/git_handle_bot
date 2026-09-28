"""Configuration loaded from environment variables (optionally via a .env file)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(Exception):
    """Raised when the configuration is missing or invalid."""


_USERNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True)
class Account:
    """One GitHub account the bot may manage, with the commit identity it should use."""

    username: str
    token: str = field(repr=False)
    email: str | None = None  # default commit email for this account
    display_name: str | None = None  # default commit name; falls back to the username


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str = field(repr=False)
    telegram_allowed_user_id: int
    github_token: str = field(repr=False)
    github_username: str
    backup_path: Path
    database_path: Path
    work_path: Path
    log_level: str = "INFO"
    git_timeout: int = 3600
    show_private_repos: bool = False
    github_api_url: str = "https://api.github.com"
    extra_accounts: tuple[Account, ...] = ()
    claude_code_path: str = "claude"
    review_timeout_ms: int = 300_000
    max_files_per_fix: int = 10
    temp_dir: Path | None = None
    github_email: str | None = None  # commit identity of the primary account
    github_name: str | None = None

    @property
    def review_dir(self) -> Path:
        """Where review clones are made (TEMP_DIR, else a folder inside WORK_PATH)."""
        return self.temp_dir or (self.work_path / "review")

    @property
    def accounts(self) -> tuple[Account, ...]:
        """All configured accounts; GITHUB_USERNAME/GITHUB_TOKEN comes first."""
        return (Account(self.github_username, self.github_token, self.github_email, self.github_name),
                *self.extra_accounts)

    def account(self, username: str) -> Account | None:
        return next((a for a in self.accounts if a.username.lower() == username.lower()), None)

    @property
    def secrets(self) -> tuple[str, ...]:
        return (self.telegram_bot_token, *(a.token for a in self.accounts))


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def load_settings(env_file: str | None = ".env") -> Settings:
    if env_file and Path(env_file).is_file():
        load_dotenv(env_file, override=False)

    bot_token = _required("TELEGRAM_BOT_TOKEN")
    if not re.fullmatch(r"\d{5,15}:[A-Za-z0-9_-]{30,}", bot_token):
        raise ConfigError("TELEGRAM_BOT_TOKEN has an invalid format")

    raw_user_id = _required("TELEGRAM_ALLOWED_USER_ID")
    if not raw_user_id.isdigit() or int(raw_user_id) <= 0:
        raise ConfigError("TELEGRAM_ALLOWED_USER_ID must be a positive integer")

    github_token = _required("GITHUB_TOKEN")
    if len(github_token) < 20 or any(c.isspace() for c in github_token):
        raise ConfigError("GITHUB_TOKEN has an invalid format")

    username = _required("GITHUB_USERNAME")
    if not _USERNAME_RE.fullmatch(username):
        raise ConfigError("GITHUB_USERNAME is not a valid GitHub username")

    backup_path = Path(_required("BACKUP_PATH")).expanduser().resolve()
    database_path = Path(os.environ.get("DATABASE_PATH", "data/bot.db")).expanduser().resolve()
    work_path = Path(os.environ.get("WORK_PATH", str(backup_path / ".work"))).expanduser().resolve()

    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise ConfigError("LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR")

    raw_timeout = os.environ.get("GIT_TIMEOUT", "3600")
    if not raw_timeout.isdigit() or not 60 <= int(raw_timeout) <= 86400:
        raise ConfigError("GIT_TIMEOUT must be an integer between 60 and 86400 seconds")

    def _identity(suffix: str) -> tuple[str | None, str | None]:
        email = os.environ.get(f"GITHUB_EMAIL{suffix}", "").strip() or None
        if email and not _EMAIL_RE.fullmatch(email):
            raise ConfigError(f"GITHUB_EMAIL{suffix} is not a valid email address")
        display = os.environ.get(f"GITHUB_NAME{suffix}", "").strip() or None
        if display and (len(display) > 80 or any(c in display for c in "<>\n")):
            raise ConfigError(f"GITHUB_NAME{suffix} is invalid")
        return email, display

    primary_email, primary_name = _identity("")

    extra: list[Account] = []
    for index in range(2, 11):  # GITHUB_USERNAME_2/GITHUB_TOKEN_2 … _10
        name = os.environ.get(f"GITHUB_USERNAME_{index}", "").strip()
        token = os.environ.get(f"GITHUB_TOKEN_{index}", "").strip()
        if not name and not token:
            continue
        if not name or not token:
            raise ConfigError(f"GITHUB_USERNAME_{index} and GITHUB_TOKEN_{index} must both be set")
        if not _USERNAME_RE.fullmatch(name):
            raise ConfigError(f"GITHUB_USERNAME_{index} is not a valid GitHub username")
        if len(token) < 20 or any(c.isspace() for c in token):
            raise ConfigError(f"GITHUB_TOKEN_{index} has an invalid format")
        if name.lower() in {username.lower(), *(a.username.lower() for a in extra)}:
            raise ConfigError(f"Duplicate account {name}")
        email, display = _identity(f"_{index}")
        extra.append(Account(name, token, email, display))

    claude_path = os.environ.get("CLAUDE_CODE_PATH", "claude").strip() or "claude"
    raw_review_timeout = os.environ.get("REVIEW_TIMEOUT_MS", "300000").strip()
    if not raw_review_timeout.isdigit() or not 10_000 <= int(raw_review_timeout) <= 3_600_000:
        raise ConfigError("REVIEW_TIMEOUT_MS must be between 10000 and 3600000")
    raw_max_files = os.environ.get("MAX_FILES_PER_FIX", "10").strip()
    if not raw_max_files.isdigit() or not 1 <= int(raw_max_files) <= 100:
        raise ConfigError("MAX_FILES_PER_FIX must be between 1 and 100")
    raw_temp = os.environ.get("TEMP_DIR", "").strip()
    temp_dir = Path(raw_temp).expanduser().resolve() if raw_temp else None

    raw_private = os.environ.get("SHOW_PRIVATE_REPOS", "false").strip().lower()
    if raw_private not in {"true", "false", "1", "0", "yes", "no"}:
        raise ConfigError("SHOW_PRIVATE_REPOS must be true or false")

    for path in (backup_path, database_path.parent, work_path, temp_dir):
        if path is not None:
            path.mkdir(parents=True, exist_ok=True)

    return Settings(
        telegram_bot_token=bot_token,
        telegram_allowed_user_id=int(raw_user_id),
        github_token=github_token,
        github_username=username,
        github_email=primary_email,
        github_name=primary_name,
        backup_path=backup_path,
        database_path=database_path,
        work_path=work_path,
        log_level=log_level,
        git_timeout=int(raw_timeout),
        show_private_repos=raw_private in {"true", "1", "yes"},
        extra_accounts=tuple(extra),
        claude_code_path=claude_path,
        review_timeout_ms=int(raw_review_timeout),
        max_files_per_fix=int(raw_max_files),
        temp_dir=temp_dir,
    )
