"""Logging with mandatory secret redaction."""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterable

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"\bbot\d{5,15}:[A-Za-z0-9_-]{30,}"), "bot[REDACTED_TELEGRAM_TOKEN]"),
    (re.compile(r"\b\d{5,15}:[A-Za-z0-9_-]{30,}"), "[REDACTED_TELEGRAM_TOKEN]"),
    (re.compile(r"(?i)(authorization:\s*)(basic|bearer|token)\s+\S+"), r"\1\2 [REDACTED]"),
    (re.compile(r"(?i)(https?://)[^/\s@]+@"), r"\1[REDACTED]@"),
)


class Redactor:
    """Removes known secrets and token-shaped strings from text."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets: list[str] = []
        for secret in secrets:
            self.add_secret(secret)

    def add_secret(self, secret: str | None) -> None:
        if secret and len(secret) >= 6 and secret not in self._secrets:
            self._secrets.append(secret)
            self._secrets.sort(key=len, reverse=True)

    def __call__(self, text: object) -> str:
        result = str(text)
        for secret in self._secrets:
            result = result.replace(secret, "[REDACTED]")
        for pattern, replacement in _PATTERNS:
            result = pattern.sub(replacement, result)
        return result


_redactor = Redactor()


def get_redactor() -> Redactor:
    return _redactor


class RedactingFilter(logging.Filter):
    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self._redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken format string must not leak args
            message = str(record.msg)
        record.msg = self._redactor(message)
        record.args = None
        if record.exc_info:
            text = logging.Formatter().formatException(record.exc_info)
            record.exc_text = self._redactor(text)
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = self._redactor(record.exc_text)
        if record.stack_info:
            record.stack_info = self._redactor(record.stack_info)
        return True


def setup_logging(level: str, secrets: Iterable[str]) -> Redactor:
    for secret in secrets:
        _redactor.add_secret(secret)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter(_redactor))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    # httpx logs full request URLs, and Telegram URLs contain the bot token.
    for noisy in ("httpx", "httpcore", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return _redactor
