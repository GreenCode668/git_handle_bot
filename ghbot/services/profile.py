"""Public GitHub profile fields (PATCH /user) with validation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from ghbot.validators import ValidationError


@dataclass(frozen=True)
class ProfileField:
    key: str
    label: str
    max_len: int
    hint: str


PROFILE_FIELDS: dict[str, ProfileField] = {
    f.key: f
    for f in (
        ProfileField("name", "Name", 255, "Display name"),
        ProfileField("bio", "Bio", 160, "Short biography (max 160 chars)"),
        ProfileField("company", "Company", 255, "e.g. @github or Acme Inc."),
        ProfileField("location", "Location", 255, "e.g. Tokyo, Japan"),
        ProfileField("blog", "Website", 255, "https://example.com"),
        ProfileField("email", "Public email", 255, "Must be a verified email on your account"),
        ProfileField("twitter_username", "X / Twitter", 15, "Username without @"),
        ProfileField("hireable", "Available for hire", 3, "yes or no"),
    )
}

CLEAR_TOKEN = "-"


def normalize_profile_value(key: str, raw: str) -> Any:
    """Validate a user-supplied value. '-' clears the field."""
    spec = PROFILE_FIELDS.get(key)
    if spec is None:
        raise ValidationError("Unsupported profile field.")
    text = raw.strip()
    if any(ord(c) < 32 and c not in "\n" for c in text):
        raise ValidationError("Control characters are not allowed.")
    if key == "hireable":
        lowered = text.lower()
        if lowered in {"yes", "y", "true", "1"}:
            return True
        if lowered in {"no", "n", "false", "0", CLEAR_TOKEN}:
            return False
        raise ValidationError("Reply with yes or no.")
    if text == CLEAR_TOKEN:
        return "" if key != "twitter_username" else None
    if len(text) > spec.max_len:
        raise ValidationError(f"{spec.label} must be at most {spec.max_len} characters.")
    if key != "bio" and "\n" in text:
        raise ValidationError("Line breaks are only allowed in the bio.")
    if key == "blog":
        parts = urlsplit(text if "://" in text else f"https://{text}")
        if parts.scheme not in {"http", "https"} or not parts.hostname or "." not in parts.hostname:
            raise ValidationError("Website must be a valid http(s) URL.")
        return text
    if key == "email" and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", text):
        raise ValidationError("Invalid email address.")
    if key == "twitter_username":
        text = text.lstrip("@")
        if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", text):
            raise ValidationError("Invalid X/Twitter username.")
    return text


def validate_social_url(url: str) -> str:
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or "." not in parts.hostname or len(url) > 255:
        raise ValidationError("Social links must be full https:// URLs.")
    if parts.username or parts.password:
        raise ValidationError("URLs must not contain credentials.")
    return url


def display(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)
