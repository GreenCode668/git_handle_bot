"""Profile README badges, managed inside a dedicated marker section only."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from ghbot.validators import ValidationError

START = "<!-- GHBOT:BADGES:START - managed by the Telegram bot, edits here are overwritten -->"
END = "<!-- GHBOT:BADGES:END -->"
_START_RE = re.compile(r"<!--\s*GHBOT:BADGES:START.*?-->")
_END_RE = re.compile(r"<!--\s*GHBOT:BADGES:END\s*-->")

TECH_PRESETS: dict[str, tuple[str, str]] = {
    # name: (simple-icons logo slug, hex color)
    "Python": ("python", "3776AB"), "JavaScript": ("javascript", "F7DF1E"), "TypeScript": ("typescript", "3178C6"),
    "Go": ("go", "00ADD8"), "Rust": ("rust", "000000"), "Java": ("openjdk", "ED8B00"), "C++": ("cplusplus", "00599C"),
    "C#": ("dotnet", "512BD4"), "PHP": ("php", "777BB4"), "Ruby": ("ruby", "CC342D"), "Kotlin": ("kotlin", "7F52FF"),
    "Swift": ("swift", "F05138"), "Dart": ("dart", "0175C2"), "React": ("react", "20232A"), "Vue.js": ("vuedotjs", "4FC08D"),
    "Node.js": ("nodedotjs", "339933"), "Django": ("django", "092E20"), "FastAPI": ("fastapi", "009688"),
    "Docker": ("docker", "2496ED"), "Kubernetes": ("kubernetes", "326CE5"), "Linux": ("linux", "FCC624"),
    "PostgreSQL": ("postgresql", "4169E1"), "MySQL": ("mysql", "4479A1"), "MongoDB": ("mongodb", "47A248"),
    "Redis": ("redis", "DC382D"), "AWS": ("amazonwebservices", "232F3E"), "Git": ("git", "F05032"),
    "Telegram": ("telegram", "26A5E4"),
}

KINDS = ("tech", "stats", "top_langs", "streak", "followers", "stars", "views", "custom")


@dataclass
class Badge:
    id: int
    kind: str
    label: str
    config: dict[str, Any]


def _shields_escape(text: str) -> str:
    return quote(text.replace("-", "--").replace("_", "__"), safe="")


def _check_text(value: str, name: str, max_len: int = 40) -> str:
    value = value.strip()
    if not value or len(value) > max_len or any(c in value for c in "<>[]()\"'`\n\r"):
        raise ValidationError(f"Invalid {name}.")
    return value


def _check_color(value: str) -> str:
    value = value.strip().lstrip("#")
    if not re.fullmatch(r"[0-9A-Fa-f]{3,8}|[a-z]{3,20}", value):
        raise ValidationError("Color must be a hex value (e.g. 3776AB) or a named color.")
    return value


def build_badge_config(kind: str, username: str, **opts: str) -> tuple[str, dict[str, Any]]:
    """Validate options and return (label, config) for storage."""
    if kind == "tech":
        name = _check_text(opts["name"], "technology name")
        preset = TECH_PRESETS.get(name)
        logo = opts.get("logo") or (preset[0] if preset else name.lower())
        if not re.fullmatch(r"[a-z0-9.+-]{1,40}", logo):
            raise ValidationError("Invalid logo slug.")
        color = _check_color(opts.get("color") or (preset[1] if preset else "555555"))
        return name, {"name": name, "logo": logo, "color": color}
    if kind == "custom":
        label = _check_text(opts["label"], "label")
        message = _check_text(opts["message"], "message")
        color = _check_color(opts.get("color") or "blue")
        return f"{label}: {message}", {"label": label, "message": message, "color": color}
    if kind in {"stats", "top_langs", "streak", "followers", "stars", "views"}:
        return kind.replace("_", " ").title(), {"username": username}
    raise ValidationError("Unknown badge type.")


def render_badge(badge: Badge) -> str:
    c = badge.config
    user = quote(c.get("username", ""), safe="")
    if badge.kind == "tech":
        url = (f"https://img.shields.io/badge/{_shields_escape(c['name'])}-{c['color']}"
               f"?style=for-the-badge&logo={quote(c['logo'])}&logoColor=white")
        return f"![{c['name']}]({url})"
    if badge.kind == "custom":
        url = f"https://img.shields.io/badge/{_shields_escape(c['label'])}-{_shields_escape(c['message'])}-{c['color']}"
        return f"![{c['label']}]({url})"
    if badge.kind == "followers":
        return f"![GitHub followers](https://img.shields.io/github/followers/{user}?style=social)"
    if badge.kind == "stars":
        return f"![GitHub stars](https://img.shields.io/github/stars/{user}?affiliations=OWNER&style=social)"
    if badge.kind == "views":
        return f"![Profile views](https://komarev.com/ghpvc/?username={user}&color=blue)"
    if badge.kind == "stats":
        return f"![GitHub stats](https://github-readme-stats.vercel.app/api?username={user}&show_icons=true)"
    if badge.kind == "top_langs":
        return f"![Top languages](https://github-readme-stats.vercel.app/api/top-langs/?username={user}&layout=compact)"
    if badge.kind == "streak":
        return f"![GitHub streak](https://streak-stats.demolab.com/?user={user})"
    raise ValueError(f"unknown badge kind {badge.kind}")


def render_section(badges: list[Badge]) -> str:
    inline = [render_badge(b) for b in badges if b.kind in {"tech", "custom", "followers", "stars", "views"}]
    cards = [render_badge(b) for b in badges if b.kind in {"stats", "top_langs", "streak"}]
    body: list[str] = []
    if inline:
        body.append(" ".join(inline))
    if cards:
        body.append("\n\n".join(cards))
    return "\n".join([START, "", *(["\n\n".join(body), ""] if body else []), END])


def apply_section(readme: str, badges: list[Badge]) -> str:
    """Replace only the managed section; append it if absent. Other content is untouched."""
    section = render_section(badges)
    starts = list(_START_RE.finditer(readme))
    ends = list(_END_RE.finditer(readme))
    if len(starts) > 1 or len(ends) > 1:
        raise ValidationError("README contains multiple badge sections; fix it manually first.")
    if starts and ends:
        s, e = starts[0], ends[0]
        if e.start() < s.end():
            raise ValidationError("README badge markers are out of order; fix it manually first.")
        return readme[: s.start()] + section + readme[e.end():]
    if starts or ends:
        raise ValidationError("README has an unmatched badge marker; fix it manually first.")
    if not readme.strip():
        return section + "\n"
    separator = "" if readme.endswith("\n\n") else ("\n" if readme.endswith("\n") else "\n\n")
    return readme + separator + section + "\n"


def outside_section(readme: str) -> str:
    """Content outside the managed section (used to prove nothing else changed)."""
    s = _START_RE.search(readme)
    e = _END_RE.search(readme)
    if s and e:
        return readme[: s.start()] + readme[e.end():]
    return readme
