"""Strict validation for every piece of user input that reaches GitHub or git."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlsplit


class ValidationError(ValueError):
    """User input failed validation. The message is safe to show to the user."""


_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_BACKUP_ID_RE = re.compile(r"^BK-\d{8}-\d{3,6}$")
_HOST_RE = re.compile(r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$")
_URL_PATH_RE = re.compile(r"^/[A-Za-z0-9._~/%+-]+$")
_REPO_ACTION_RE = re.compile(
    r"^(?P<repo>[A-Za-z0-9._-]{1,100}(?:/[A-Za-z0-9._-]{1,100})?)\s+"
    r"@(?P<action>remove|delete|rename|private|public|archive|unarchive|info|commits|branches|actions|backup|stats)"
    r"(?:\s+(?P<arg>\S+))?\s*$"
)

GIT_EPOCH = date(2005, 4, 7)
REPO_ACTIONS = ("remove", "rename", "private", "public", "archive", "unarchive", "info",
                "commits", "branches", "actions", "backup", "stats")


@dataclass(frozen=True)
class RepoRef:
    owner: str
    name: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def key(self) -> str:
        """Case-insensitive identity (GitHub repo names are case-insensitive)."""
        return self.full_name.lower()

    def __str__(self) -> str:
        return self.full_name


def validate_owner(owner: str) -> str:
    owner = owner.strip()
    if not _OWNER_RE.fullmatch(owner):
        raise ValidationError("Invalid GitHub owner name.")
    return owner


def validate_repo_name(name: str) -> str:
    name = name.strip()
    if not _REPO_RE.fullmatch(name) or name in {".", ".."}:
        raise ValidationError(
            "Invalid repository name. Use 1-100 characters: letters, digits, '.', '_' or '-'."
        )
    if name.lower().endswith(".git"):
        raise ValidationError("Repository names must not end with '.git'.")
    return name


def parse_repo(text: str, default_owner: str) -> RepoRef:
    """Parse 'name' or 'owner/name'. Only the configured account may be managed."""
    text = text.strip()
    if "/" in text:
        owner, _, name = text.partition("/")
        owner = validate_owner(owner)
    else:
        owner, name = default_owner, text
    name = validate_repo_name(name)
    if owner.lower() != default_owner.lower():
        raise ValidationError(f"This bot only manages repositories owned by {default_owner}.")
    return RepoRef(default_owner, name)


def validate_branch_name(branch: str) -> str:
    """Mirror of `git check-ref-format --branch` rules."""
    b = branch.strip()
    bad = (
        not b
        or len(b) > 255
        or b.startswith(("-", "/", "."))
        or b.endswith(("/", ".", ".lock"))
        or ".." in b
        or "//" in b
        or "@{" in b
        or b == "@"
        or "/." in b
        or any(ord(c) < 32 or ord(c) == 127 or c in " ~^:?*[\\" for c in b)
    )
    if bad:
        raise ValidationError("Invalid branch name.")
    return b


def parse_date(text: str, *, today: date | None = None) -> date:
    text = text.strip()
    match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if not match:
        raise ValidationError("Dates must use the format YYYY-MM-DD, for example 2013-01-01.")
    try:
        value = date(int(match[1]), int(match[2]), int(match[3]))
    except ValueError as exc:
        raise ValidationError("That date does not exist.") from exc
    today = today or datetime.now(UTC).date()
    if value < GIT_EPOCH or value > today + timedelta(days=1):
        raise ValidationError(f"Date must be between {GIT_EPOCH} and today.")
    return value


def cutoff_datetime(value: date) -> datetime:
    """The cutoff instant: midnight UTC at the start of the given day."""
    return datetime(value.year, value.month, value.day, tzinfo=UTC)


def validate_git_url(url: str) -> str:
    """Accept only credential-free public HTTPS git URLs."""
    url = url.strip()
    if len(url) > 500 or any(ord(c) < 33 or ord(c) == 127 for c in url):
        raise ValidationError("Invalid repository URL.")
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise ValidationError("Only https:// repository URLs are supported.")
    if parts.username or parts.password or "@" in parts.netloc:
        raise ValidationError("URLs must not contain credentials.")
    if parts.query or parts.fragment:
        raise ValidationError("URLs must not contain a query string or fragment.")
    host = (parts.hostname or "").lower()
    if parts.port not in (None, 443):
        raise ValidationError("Only the default HTTPS port is supported.")
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        raise ValidationError("IP address URLs are not allowed; use a hostname.")
    if host in {"localhost"} or host.endswith((".localhost", ".local", ".internal")):
        raise ValidationError("Local hostnames are not allowed.")
    if not _HOST_RE.fullmatch(host):
        raise ValidationError("Invalid hostname in URL.")
    if not _URL_PATH_RE.fullmatch(parts.path or "") or ".." in parts.path or parts.path == "/":
        raise ValidationError("Invalid repository path in URL.")
    return url


def suggest_repo_name(url: str) -> str:
    last = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    if last.lower().endswith(".git"):
        last = last[:-4]
    candidate = re.sub(r"[^A-Za-z0-9._-]", "-", last)[:100].strip(".") or "imported-repo"
    try:
        return validate_repo_name(candidate)
    except ValidationError:
        return "imported-repo"


def validate_backup_id(text: str) -> str:
    text = text.strip().upper()
    if not _BACKUP_ID_RE.fullmatch(text):
        raise ValidationError("Invalid backup ID. Expected a value like BK-20260915-001.")
    return text


@dataclass(frozen=True)
class RepoAction:
    repo: str
    action: str
    arg: str | None


def parse_repo_action(text: str) -> RepoAction | None:
    """Parse 'repo-name @action [arg]'. Returns None when the text is not an action."""
    match = _REPO_ACTION_RE.fullmatch(text.strip())
    if not match:
        return None
    action = "remove" if match["action"] == "delete" else match["action"]
    return RepoAction(match["repo"], action, match["arg"])


def confirmation_phrase(verb: str, target: str) -> str:
    return f"{verb} {target}"


def matches_confirmation(text: str, expected: str) -> bool:
    """Exact, case-sensitive comparison (only surrounding whitespace is ignored)."""
    return text.strip() == expected


def validate_page(value: object, *, maximum: int = 10_000) -> int:
    try:
        page = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValidationError("Invalid page number.") from exc
    if not 1 <= page <= maximum:
        raise ValidationError("Invalid page number.")
    return page


_TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,49}$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{4,40}$")


def validate_description(text: str) -> str:
    text = text.strip()
    if text == "-":
        return ""
    if len(text) > 350 or any(ord(c) < 32 for c in text):
        raise ValidationError("Description must be a single line of at most 350 characters.")
    return text


def validate_homepage(text: str) -> str:
    text = text.strip()
    if text in ("", "-"):
        return ""
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname or "." not in parts.hostname:
        raise ValidationError("Homepage must be a full http(s):// URL.")
    if parts.username or parts.password or len(text) > 255 or any(ord(c) < 33 for c in text):
        raise ValidationError("Invalid homepage URL.")
    return text


def validate_topic(topic: str) -> str:
    topic = topic.strip().lower()
    if not _TOPIC_RE.fullmatch(topic):
        raise ValidationError(f"Invalid topic '{topic[:50]}'. Use lowercase letters, digits and hyphens (max 50).")
    return topic


def validate_topics(topics: list[str]) -> list[str]:
    result = sorted({validate_topic(t) for t in topics})
    if len(result) > 20:
        raise ValidationError("A repository can have at most 20 topics.")
    return result


def validate_ref(ref: str) -> str:
    """A branch, tag or commit SHA used in read-only lookups."""
    ref = ref.strip()
    if _SHA_RE.fullmatch(ref):
        return ref
    return validate_branch_name(ref)


def validate_sha(sha: str) -> str:
    sha = sha.strip()
    if not _SHA_RE.fullmatch(sha):
        raise ValidationError("Commit SHA must be 4-40 hexadecimal characters.")
    return sha


def validate_run_id(text: str) -> int:
    text = str(text).strip()
    if not text.isdigit() or not 0 < int(text) < 10**15:
        raise ValidationError("Run id must be a positive number.")
    return int(text)


def validate_search(text: str) -> str:
    text = text.strip()
    if not 1 <= len(text) <= 100 or any(ord(c) < 32 for c in text):
        raise ValidationError("Search text must be 1-100 characters.")
    return text
