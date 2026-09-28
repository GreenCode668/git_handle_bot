import httpx
import pytest

from ghbot.db import Database, RepoLockedError
from ghbot.github.client import GitHubClient, GitHubError, GitHubNotFound
from ghbot.services.badges import END, START, Badge, apply_section, build_badge_config, outside_section, render_badge
from ghbot.services.profile import normalize_profile_value
from ghbot.validators import ValidationError


# ------------------------------------------------------------------ database
async def test_backup_ids_are_sequential_per_day(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    ids = [await db.next_backup_id() for _ in range(3)]
    assert [i[-3:] for i in ids] == ["001", "002", "003"]
    assert len({i[3:11] for i in ids}) == 1
    db.close()


async def test_locks_are_exclusive(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    await db.acquire_lock("me/Repo", "op1", 1)
    with pytest.raises(RepoLockedError):
        await db.acquire_lock("ME/repo", "op2", 2)  # case-insensitive
    await db.release_lock("me/repo", 2)  # wrong owner does not release
    with pytest.raises(RepoLockedError):
        await db.acquire_lock("me/repo", "op2", 2)
    await db.release_lock("me/repo", 1)
    await db.acquire_lock("me/repo", "op2", 2)
    db.close()


async def test_transition_is_compare_and_set(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    op = await db.create_operation("x", "me/r", "analyzed")
    assert await db.transition(op.id, ["analyzed"], "executing")
    assert not await db.transition(op.id, ["analyzed"], "executing")
    db.close()


# -------------------------------------------------------------------- badges
def badge(kind, **opts):
    label, config = build_badge_config(kind, "me", **opts)
    return Badge(1, kind, label, config)


def test_badge_section_preserves_other_content():
    readme = "# Hello\n\nSome intro.\n\n## Projects\n- a\n"
    first = apply_section(readme, [badge("tech", name="Python")])
    assert first.startswith(readme) and START in first and END in first
    second = apply_section(first, [badge("tech", name="Go"), badge("stats")])
    assert outside_section(second) == outside_section(first)
    assert "Python" not in second and "logo=go" in second
    edited = first.replace("Some intro.", "Changed intro by hand.")
    assert "Changed intro by hand." in apply_section(edited, [])


def test_badge_section_in_middle_is_replaced_in_place():
    readme = f"top\n{START}\nold\n{END}\nbottom\n"
    out = apply_section(readme, [badge("followers")])
    assert out.startswith("top\n") and out.endswith("\nbottom\n") and "old" not in out


def test_badge_markers_must_be_well_formed():
    for broken in (f"{START}\nno end", f"{END}\n{START}", f"{START}{END}{START}{END}"):
        with pytest.raises(ValidationError):
            apply_section(broken, [])


def test_badge_input_validation_blocks_markdown_injection():
    with pytest.raises(ValidationError):
        build_badge_config("custom", "me", label="x](http://evil)", message="m")
    with pytest.raises(ValidationError):
        build_badge_config("tech", "me", name="X", logo="bad logo", color="fff")
    assert "C%2B%2B" in render_badge(badge("tech", name="C++"))
    assert "a--b" in render_badge(badge("custom", label="a-b", message="c", color="red"))


# ------------------------------------------------------------------- profile
def test_profile_normalization():
    assert normalize_profile_value("hireable", "Yes") is True
    assert normalize_profile_value("bio", "-") == ""
    assert normalize_profile_value("twitter_username", "@me_1") == "me_1"
    assert normalize_profile_value("twitter_username", "-") is None
    with pytest.raises(ValidationError):
        normalize_profile_value("bio", "x" * 161)
    with pytest.raises(ValidationError):
        normalize_profile_value("blog", "javascript:alert(1)")
    with pytest.raises(ValidationError):
        normalize_profile_value("login", "x")


# ------------------------------------------------------------- github client
async def test_client_pagination_and_errors():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer tok_" + "x" * 20
        if request.url.path == "/missing":
            return httpx.Response(404, json={"message": "Not Found"})
        if request.url.path == "/bad":
            return httpx.Response(422, json={"message": "Validation Failed", "errors": [{"message": "name already exists"}]})
        page = int(request.url.params.get("page", 1))
        link = '<https://api.github.com/items?page=2>; rel="next", <https://api.github.com/items?page=2>; rel="last"' if page == 1 else ""
        return httpx.Response(200, json=[{"n": page}], headers={"Link": link, "X-RateLimit-Remaining": "99"})

    client = GitHubClient("tok_" + "x" * 20, "https://api.github.com", transport=httpx.MockTransport(handler))
    assert await client.paginate("/items") == [{"n": 1}, {"n": 2}]
    page = await client.page("/items", page=1, per_page=1)
    assert page.has_next and page.last_page == 2
    assert client.rate_remaining == 99
    with pytest.raises(GitHubNotFound):
        await client.get("/missing")
    with pytest.raises(GitHubError, match="name already exists"):
        await client.request("POST", "/bad")
    await client.close()
