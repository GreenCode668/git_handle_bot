"""Password lock for the Telegram session.

The bot starts locked: until the correct password is sent, no command, button or
text is processed. Only a scrypt hash and a random salt are stored (in SQLite);
the password itself is never written to disk or logged.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import hmac
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from ghbot.db import parse_iso, utcnow
from ghbot.validators import ValidationError

if TYPE_CHECKING:
    from ghbot.services.container import Services

ALGORITHM = "scrypt"
# ~32 MB and ~0.1 s per attempt: slow for guessing, fine for a single user.
SCRYPT_PARAMS = {"n": 2**15, "r": 8, "p": 1, "dklen": 32}
MIN_LENGTH = 8
MAX_LENGTH = 128
MAX_ATTEMPTS = 5
LOCKOUT = timedelta(minutes=15)


@dataclass
class UnlockResult:
    ok: bool
    message: str
    attempts_left: int = 0


def _derive(password: str, salt: bytes, params: dict[str, Any]) -> bytes:
    values = {k: int(v) for k, v in params.items()}
    # OpenSSL's default memory limit is below what these parameters need.
    maxmem = 128 * values["n"] * values["r"] * 2
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, maxmem=maxmem, **values)


def validate_password(password: str) -> str:
    if any(ord(c) < 32 or ord(c) == 127 for c in password):
        raise ValidationError("The password must not contain control characters.")
    if not MIN_LENGTH <= len(password) <= MAX_LENGTH:
        raise ValidationError(f"The password must be {MIN_LENGTH}-{MAX_LENGTH} characters long.")
    if password.strip() != password:
        raise ValidationError("The password must not start or end with spaces.")
    return password


class PasswordLock:
    """Session lock state. Locked again whenever the bot process restarts."""

    def __init__(self, services: Services) -> None:
        self.s = services
        self._unlocked = False
        self._last_activity = utcnow()

    # ------------------------------------------------------------- state
    async def is_configured(self) -> bool:
        return await self.s.db.get_auth() is not None

    @property
    def unlocked(self) -> bool:
        return self._unlocked

    def lock(self) -> None:
        self._unlocked = False

    def touch(self) -> None:
        self._last_activity = utcnow()

    async def autolock_minutes(self) -> int:
        return int(await self.s.db.get_setting("auth_autolock_minutes", 0))

    async def check_autolock(self) -> bool:
        """Lock again after inactivity. Returns True when it just locked."""
        minutes = await self.autolock_minutes()
        if self._unlocked and minutes and utcnow() - self._last_activity >= timedelta(minutes=minutes):
            self._unlocked = False
            return True
        return False

    async def lockout_remaining(self) -> timedelta | None:
        auth = await self.s.db.get_auth()
        locked_until = parse_iso(auth["locked_until"]) if auth else None
        if locked_until and locked_until > utcnow():
            return locked_until - utcnow()
        return None

    # --------------------------------------------------------- operations
    async def set_password(self, new_password: str) -> None:
        validate_password(new_password)
        salt = secrets.token_bytes(16)
        digest = await asyncio.to_thread(_derive, new_password, salt, SCRYPT_PARAMS)
        await self.s.db.set_auth(ALGORITHM, salt, digest, SCRYPT_PARAMS)
        self._unlocked = True
        self.touch()

    async def change_password(self, current: str, new_password: str) -> None:
        if not await self.verify(current):
            raise ValidationError("The current password is not correct. The password was not changed.")
        if current == new_password:
            raise ValidationError("The new password must be different from the current one.")
        await self.set_password(new_password)

    async def verify(self, password: str) -> bool:
        auth = await self.s.db.get_auth()
        if auth is None:
            return False
        params = json.loads(auth["params"])
        digest = await asyncio.to_thread(_derive, password, auth["salt"], params)
        return hmac.compare_digest(digest, auth["hash"])

    async def unlock(self, password: str) -> UnlockResult:
        remaining = await self.lockout_remaining()
        if remaining is not None:
            return UnlockResult(False, f"Too many wrong attempts. Try again in {int(remaining.total_seconds() // 60) + 1} minute(s).")
        auth = await self.s.db.get_auth()
        if auth is None:
            return UnlockResult(False, "No password is set yet.")
        if await self.verify(password):
            await self.s.db.set_auth_failures(0, None)
            self._unlocked = True
            self.touch()
            return UnlockResult(True, "Unlocked.")
        attempts = int(auth["failed_attempts"]) + 1
        if attempts >= MAX_ATTEMPTS:
            await self.s.db.set_auth_failures(0, utcnow() + LOCKOUT)
            return UnlockResult(False, f"Wrong password. Too many attempts: locked for {int(LOCKOUT.total_seconds() // 60)} minutes.")
        await self.s.db.set_auth_failures(attempts, None)
        left = MAX_ATTEMPTS - attempts
        return UnlockResult(False, f"Wrong password. {left} attempt(s) left before a {int(LOCKOUT.total_seconds() // 60)}-minute lockout.", left)
