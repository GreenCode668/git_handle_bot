"""Profile README ("about me"): the README of the repository named like the account."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ghbot.services.badges import Badge, render_section
from ghbot.validators import ValidationError

if TYPE_CHECKING:
    from ghbot.services.container import Services

MAX_SIZE = 200_000  # far above any sensible README, and below GitHub's contents API limit


@dataclass
class ProfileReadmeState:
    repo: str
    exists: bool
    readme: str | None
    sha: str | None

    @property
    def has_readme(self) -> bool:
        return self.readme is not None


def validate_readme(text: str) -> str:
    text = text.replace("\r\n", "\n").strip("\n")
    if not text.strip():
        raise ValidationError("The README cannot be empty.")
    if len(text.encode()) > MAX_SIZE:
        raise ValidationError(f"The README is too large (limit {MAX_SIZE // 1000} kB).")
    if "\x00" in text:
        raise ValidationError("The README contains invalid characters.")
    return text + "\n"


def render_template(username: str, user: dict[str, Any], badges: list[Badge], socials: list[str]) -> str:
    """A complete profile README built from the account's own data."""
    name = user.get("name") or username
    bio = (user.get("bio") or "").strip()
    company = (user.get("company") or "").strip()
    location = (user.get("location") or "").strip()
    blog = (user.get("blog") or "").strip()

    facts = []
    if company:
        facts.append(f"🏢 {company}")
    if location:
        facts.append(f"📍 {location}")
    if blog:
        facts.append(f"🔗 [{blog.replace('https://', '').replace('http://', '')}]({blog})")
    for url in socials[:5]:
        facts.append(f"💬 [{url.split('//')[-1].split('/')[0]}]({url})")

    parts = [
        f"<h1 align=\"center\">{name}</h1>",
        "",
        f"<p align=\"center\">{bio or 'Software engineer'}</p>",
        "",
        render_section(badges) if badges else "",
        "",
        "## About me",
        "",
        bio or f"Hi, I am {name}. I build and maintain software projects on GitHub.",
        "",
    ]
    if facts:
        parts += ["### Where to find me", "", *[f"- {fact}" for fact in facts], ""]
    parts += [
        "## GitHub stats",
        "",
        f"![Stats](https://github-readme-stats.vercel.app/api?username={username}&show_icons=true)",
        "",
        f"![Top languages](https://github-readme-stats.vercel.app/api/top-langs/?username={username}&layout=compact)",
        "",
    ]
    return "\n".join(line for line in parts if line is not None).strip() + "\n"


async def read_state(services: Services, repo_full_name: str) -> ProfileReadmeState:
    exists = await services.gh.repo_exists(repo_full_name)
    if not exists:
        return ProfileReadmeState(repo_full_name, False, None, None)
    current = await services.gh.get_file(repo_full_name, "README.md")
    return ProfileReadmeState(repo_full_name, True, current[0] if current else None, current[1] if current else None)


async def publish_readme(services: Services, repo_full_name: str, content: str, message: str) -> tuple[bool, str]:
    """Write README.md, keeping a snapshot of the previous content first. Returns (verified, sha)."""
    content = validate_readme(content)
    state = await read_state(services, repo_full_name)
    if not state.exists:
        raise ValidationError(f"{repo_full_name} does not exist yet.")
    if state.readme is not None:
        await services.db.save_readme_snapshot(repo_full_name, "README.md", state.sha, state.readme)
    result = await services.gh.put_file(repo_full_name, "README.md", content, message, state.sha)
    written = await services.gh.get_file(repo_full_name, "README.md")
    ok = written is not None and written[0] == content
    await services.db.log_simple("profile_readme", repo_full_name, "done" if ok else "failed",
                                 result={"verified": ok, "size": len(content)})
    return ok, (result.get("content") or {}).get("sha", "")
