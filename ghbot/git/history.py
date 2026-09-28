"""Safe commit-history cleanup.

Strategy ("tree-preserving cutoff squash")
-----------------------------------------
1. A commit is *old* when its committer date is before the cutoff AND all of
   its parents are old. This set is closed under ancestry, so removing it can
   never orphan a newer commit. Commits dated before the cutoff that descend
   from newer commits (clock skew, rebases) stay and are reported.
2. Every old commit that is still needed - the parent of a newer commit, an old
   branch tip, or (optionally) the target of an old tag - becomes a *boundary*.
   Each boundary is replaced by a parentless "squash root" commit holding the
   exact same file tree.
3. Every newer commit is re-created byte-for-byte (tree, author, committer,
   dates, encoding, message) with only its parent pointers remapped. Commit
   signatures are dropped because they can no longer be valid.
4. Verification: git fsck, every branch tip's tree hash equals the original,
   no old commit remains reachable, and the reachable commit count is exactly
   as planned. Only then are refs pushed, atomically, with --force-with-lease
   pinned to the SHAs recorded in the verified backup.

Nothing in `analyze_history` writes to the repository.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import zlib
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ghbot.git.mirror import important, local_refs, object_format
from ghbot.git.runner import GitError, GitRunner

TAG_POLICIES = ("snapshot", "drop")
# "summary": pre-cutoff commits become one summary commit per boundary.
# "none":    pre-cutoff commits disappear; the first post-cutoff commits become root commits
#            (their trees already contain every older file). A branch or kept tag with no
#            post-cutoff commit still needs one snapshot commit, otherwise its files would vanish.
SQUASH_STYLES = ("summary", "none")
_SIG_RE = re.compile(rb"\n-----BEGIN (PGP|SSH) SIGNATURE-----\n.*\Z", re.S)


@dataclass
class RefInfo:
    name: str
    sha: str
    type: str
    peeled_sha: str | None
    peeled_type: str | None

    @property
    def commit(self) -> str | None:
        if self.type == "commit":
            return self.sha
        if self.type == "tag" and self.peeled_type == "commit":
            return self.peeled_sha
        return None


@dataclass
class BranchImpact:
    name: str
    tip: str
    tip_is_old: bool
    old_commits: int
    new_commits: int


@dataclass
class TagImpact:
    name: str
    annotated: bool
    status: str  # "rewritten" | "old" | "unchanged" | "unsupported"


@dataclass
class HistoryAnalysis:
    cutoff: str
    object_format: str
    total_commits: int
    dated_before: int
    dated_after: int
    squash_count: int
    rewrite_count: int
    boundary_count: int
    root_count: int
    merges_total: int
    merges_crossing_cutoff: int
    date_anomalies: int
    signed_commits_rewritten: int
    touches_workflows: bool
    uses_lfs: bool
    branches: list[BranchImpact]
    tags: list[TagImpact]
    rewrite_required: bool
    blockers: list[str] = field(default_factory=list)
    squash_style: str = "summary"
    snapshot_count: int = 0  # boundary commits that must be kept as snapshots (style "none")
    roots_after: int = 0  # starting (parentless) commits after the rewrite
    complications: list[str] = field(default_factory=list)

    @property
    def affected_branches(self) -> list[BranchImpact]:
        return [b for b in self.branches if b.old_commits > 0]

    @property
    def affected_tags(self) -> list[TagImpact]:
        return [t for t in self.tags if t.status != "unchanged"]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class _Graph:
    refs: list[RefInfo]
    order: list[str]  # topological, parents first
    parents: dict[str, list[str]]
    ctime: dict[str, int]
    old: set[str]


def _list_refs(git: GitRunner, repo: Path) -> list[RefInfo]:
    fmt = "%(refname)%00%(objectname)%00%(objecttype)%00%(*objectname)%00%(*objecttype)"
    out = git.run(["for-each-ref", f"--format={fmt}", "refs/heads", "refs/tags"], cwd=repo).stdout
    refs = []
    for line in out.splitlines():
        name, sha, typ, psha, ptyp = line.split("\x00")
        refs.append(RefInfo(name, sha, typ, psha or None, ptyp or None))
    return refs


def _build_graph(git: GitRunner, repo: Path, cutoff: datetime) -> _Graph:
    refs = _list_refs(git, repo)
    tips = sorted({r.commit for r in refs if r.commit})
    order: list[str] = []
    parents: dict[str, list[str]] = {}
    ctime: dict[str, int] = {}
    if tips:
        out = git.run_bytes(
            ["log", "--stdin", "--topo-order", "--reverse", "--format=%H %ct %P"],
            cwd=repo, input_bytes=("\n".join(tips) + "\n").encode(),
        ).decode()
        for line in out.splitlines():
            parts = line.split()
            sha, ts, ps = parts[0], int(parts[1]), parts[2:]
            order.append(sha)
            parents[sha] = ps
            ctime[sha] = ts
    limit = int(cutoff.timestamp())
    old: set[str] = set()
    for sha in order:  # parents are always visited first
        if ctime[sha] < limit and all(p in old for p in parents[sha]):
            old.add(sha)
    return _Graph(refs, order, parents, ctime, old)


def _boundaries(graph: _Graph, tag_policy: str, squash_style: str = "summary") -> set[str]:
    """Old commits that must be replaced by a parentless commit with the same tree."""
    result: set[str] = set()
    if squash_style == "summary":
        for sha in graph.order:
            if sha not in graph.old:
                result.update(p for p in graph.parents[sha] if p in graph.old)
    for ref in graph.refs:
        commit = ref.commit
        if commit in graph.old and (ref.name.startswith("refs/heads/") or tag_policy == "snapshot"):
            if ref.type == "tag" and ref.peeled_type == "tag":
                continue
            result.add(commit)
    return result


def _ancestor_count(graph: _Graph, sha: str) -> int:
    seen, stack = set(), [sha]
    while stack:
        cur = stack.pop()
        if cur not in seen:
            seen.add(cur)
            stack.extend(graph.parents.get(cur, []))
    return len(seen)


def _read_objects(git: GitRunner, repo: Path, shas: list[str]) -> dict[str, tuple[str, bytes]]:
    if not shas:
        return {}
    data = git.run_bytes(["cat-file", "--batch"], cwd=repo, input_bytes=("\n".join(shas) + "\n").encode())
    result: dict[str, tuple[str, bytes]] = {}
    pos = 0
    while pos < len(data):
        nl = data.index(b"\n", pos)
        header = data[pos:nl].decode().split()
        if len(header) < 3:
            raise GitError(f"object missing: {header[0] if header else '?'}")
        sha, typ, size = header[0], header[1], int(header[2])
        start = nl + 1
        result[sha] = (typ, data[start:start + size])
        pos = start + size + 1
    return result


def _roots_after(graph: _Graph, boundaries: set[str], squash_style: str) -> int:
    new = [s for s in graph.order if s not in graph.old]
    if squash_style == "summary":
        return len(boundaries) + sum(1 for s in new if not graph.parents[s])
    return len(boundaries) + sum(1 for s in new if all(p in graph.old for p in graph.parents[s]))


def analyze_history(git: GitRunner, repo: Path, cutoff: datetime, tag_policy: str = "snapshot",
                    squash_style: str = "summary") -> HistoryAnalysis:
    """Read-only analysis of what a cutoff squash would do."""
    if tag_policy not in TAG_POLICIES:
        raise ValueError("invalid tag policy")
    if squash_style not in SQUASH_STYLES:
        raise ValueError("invalid squash style")
    blockers: list[str] = []
    complications: list[str] = []

    fmt = object_format(git, repo)
    if fmt != "sha1":
        blockers.append(f"Unsupported object format {fmt}.")
    if (repo / "shallow").exists():
        blockers.append("Repository clone is shallow; full history is required.")

    graph = _build_graph(git, repo, cutoff)
    limit = int(cutoff.timestamp())
    new = [s for s in graph.order if s not in graph.old]
    boundaries = _boundaries(graph, tag_policy, squash_style)
    dated_before = sum(1 for s in graph.order if graph.ctime[s] < limit)
    anomalies = dated_before - len(graph.old)
    roots = [s for s in graph.order if not graph.parents[s]]

    # Per-branch impact.
    branches: list[BranchImpact] = []
    for ref in graph.refs:
        if not ref.name.startswith("refs/heads/") or not ref.commit:
            continue
        reach = git.run(["rev-list", ref.commit], cwd=repo).stdout.split()
        old_n = sum(1 for s in reach if s in graph.old)
        branches.append(BranchImpact(ref.name.removeprefix("refs/heads/"), ref.commit,
                                     ref.commit in graph.old, old_n, len(reach) - old_n))

    tags: list[TagImpact] = []
    for ref in graph.refs:
        if not ref.name.startswith("refs/tags/"):
            continue
        name = ref.name.removeprefix("refs/tags/")
        annotated = ref.type == "tag"
        if ref.type == "tag" and ref.peeled_type == "tag":
            tags.append(TagImpact(name, True, "unsupported"))
        elif ref.commit is None:
            tags.append(TagImpact(name, annotated, "unchanged"))
        elif ref.commit in graph.old:
            tags.append(TagImpact(name, annotated, "old"))
        elif graph.old:
            tags.append(TagImpact(name, annotated, "rewritten"))
        else:
            tags.append(TagImpact(name, annotated, "unchanged"))

    merges_total = sum(1 for s in graph.order if len(graph.parents[s]) > 1)
    crossing = sum(
        1 for s in new if len(graph.parents[s]) > 1 and any(p in graph.old for p in graph.parents[s])
    )

    signed = 0
    if graph.old and new:
        objects = _read_objects(git, repo, new)
        signed = sum(1 for _, body in objects.values() if b"\ngpgsig" in body.split(b"\n\n", 1)[0])

    touches_workflows = False
    uses_lfs = False
    tips = sorted({r.commit for r in graph.refs if r.commit})
    if tips and new and graph.old:
        out = git.run(["log", "--format=%H", "-1", *tips, "--", ".github/workflows"], cwd=repo).stdout
        touches_workflows = bool(out.strip())
    for ref in graph.refs:
        if ref.name.startswith("refs/heads/"):
            attr = git.run(["cat-file", "-p", f"{ref.sha}:.gitattributes"], cwd=repo, check=False)
            if attr.returncode == 0 and "filter=lfs" in attr.stdout:
                uses_lfs = True

    rewrite_required = any(graph.parents[s] or s not in boundaries for s in graph.old)

    if anomalies:
        complications.append(
            f"{anomalies} commit(s) are dated before the cutoff but descend from newer commits "
            "(clock skew or rebases). They are kept."
        )
    if crossing:
        if squash_style == "none":
            complications.append(f"{crossing} merge commit(s) lose their pre-cutoff parent (their files are unchanged).")
        else:
            complications.append(f"{crossing} merge commit(s) join pre-cutoff and post-cutoff history.")
    roots_after = _roots_after(graph, boundaries, squash_style)
    if squash_style == "summary" and len(boundaries) > 1:
        complications.append(
            f"{len(boundaries)} separate squash roots are needed (branches or tags diverged before the cutoff)."
        )
    if squash_style == "none" and graph.old:
        stale = [b.name for b in branches if b.tip_is_old]
        if stale:
            complications.append(
                "Branches with no commits after the cutoff keep ONE snapshot commit (otherwise their files would be lost): "
                + ", ".join(stale)
            )
        if roots_after > 1:
            complications.append(f"History will have {roots_after} starting commits (branches diverged before the cutoff).")
    if len(roots) > 1:
        complications.append(f"Repository has {len(roots)} root commits (orphan branches or merged histories).")
    if signed:
        complications.append(f"{signed} signed commit(s) will lose their signatures (they cannot stay valid).")
    old_tags = [t for t in tags if t.status == "old"]
    if old_tags:
        action = "kept as snapshot commits" if tag_policy == "snapshot" else "deleted"
        complications.append(f"{len(old_tags)} tag(s) point to pre-cutoff commits and will be {action}.")
    unsupported = [t for t in tags if t.status == "unsupported"]
    if unsupported and rewrite_required:
        complications.append(f"{len(unsupported)} nested tag(s) cannot be rewritten and will be deleted.")
    if touches_workflows and rewrite_required:
        complications.append("Rewritten commits touch .github/workflows: the token needs the 'workflow' scope.")
    if uses_lfs:
        complications.append("Git LFS is used: LFS objects are untouched, but pointers in history are rewritten.")
    if rewrite_required:
        complications.append(
            "Anyone with a clone must re-clone; open pull requests may break; old SHAs can stay "
            "cached on GitHub (forks, PR refs) until GitHub garbage-collects them."
        )

    return HistoryAnalysis(
        cutoff=cutoff.isoformat(),
        object_format=fmt,
        total_commits=len(graph.order),
        dated_before=dated_before,
        dated_after=len(graph.order) - dated_before,
        squash_count=len(graph.old),
        rewrite_count=len(new) if graph.old else 0,
        boundary_count=len(boundaries),
        root_count=len(roots),
        merges_total=merges_total,
        merges_crossing_cutoff=crossing,
        date_anomalies=anomalies,
        signed_commits_rewritten=signed,
        touches_workflows=touches_workflows,
        uses_lfs=uses_lfs,
        branches=branches,
        tags=tags,
        rewrite_required=rewrite_required and not blockers,
        blockers=blockers,
        complications=complications,
        squash_style=squash_style,
        snapshot_count=len(boundaries),
        roots_after=roots_after,
    )


# ------------------------------------------------------------------- authorship
_IDENT_RE = re.compile(rb"^(?P<name>.*) <(?P<email>[^>]*)> (?P<rest>\d+ [+-]?\d{4})$")


@dataclass
class AuthorInfo:
    name: str
    email: str
    commits: int

    @property
    def key(self) -> str:
        return f"{self.name} <{self.email}>"


def author_stats(git: GitRunner, repo: Path) -> list[AuthorInfo]:
    """Every author identity in the reachable history, most commits first (read-only)."""
    out = git.run(["log", "--branches", "--tags", "--format=%aN%x00%aE"], cwd=repo, check=False).stdout
    counts: dict[tuple[str, str], int] = {}
    for line in out.splitlines():
        name, _, email = line.partition("\x00")
        if name or email:
            counts[(name, email)] = counts.get((name, email), 0) + 1
    return [AuthorInfo(name, email, count)
            for (name, email), count in sorted(counts.items(), key=lambda item: -item[1])]


def _matches(value: bytes, identities: set[tuple[str, str]]) -> bool:
    match = _IDENT_RE.match(value)
    if not match:
        return False
    name = match["name"].decode("utf-8", "replace")
    email = match["email"].decode("utf-8", "replace").lower()
    return (name, email) in identities


def _replace_identity(value: bytes, new_name: str, new_email: str) -> bytes:
    match = _IDENT_RE.match(value)
    if not match:
        return value
    return f"{new_name} <{new_email}> ".encode() + match["rest"]


def count_authored(git: GitRunner, repo: Path, identities: set[tuple[str, str]]) -> int:
    return sum(info.commits for info in author_stats(git, repo)
               if (info.name, info.email.lower()) in identities)


def rewrite_authors(git: GitRunner, repo: Path, identities: set[tuple[str, str]],
                    new_name: str, new_email: str) -> RewriteResult:
    """Rewrite author/committer identities in a disposable working mirror, then verify locally.

    Commit dates, messages and file trees are untouched; only the identity lines change.
    Every commit is re-created, so all SHAs change. Signatures are dropped (they cannot stay valid).
    """
    if object_format(git, repo) != "sha1":
        raise GitError("Unsupported object format")
    if not identities:
        raise GitError("No author identity selected.")
    if not new_email or "@" not in new_email or "<" in new_name or ">" in new_name:
        raise GitError("Invalid target identity.")

    graph = _build_graph(git, repo, datetime.fromtimestamp(0, UTC))  # cutoff unused: nothing is "old"
    objects = _read_objects(git, repo, list(graph.order))
    matched = 0
    mapping: dict[str, str] = {}
    for sha in graph.order:
        headers, message = _split_headers(objects[sha][1])
        out: list[tuple[bytes, bytes]] = []
        touched = False
        for key, value in headers:
            if key == b"tree":
                out.append((key, value))
                out.extend((b"parent", mapping[p].encode()) for p in graph.parents[sha])
            elif key == b"parent":
                continue
            elif key in (b"gpgsig", b"gpgsig-sha256"):
                continue
            elif key in (b"author", b"committer") and _matches(value, identities):
                out.append((key, _replace_identity(value, new_name, new_email)))
                touched = touched or key == b"author"
            else:
                out.append((key, value))
        matched += int(touched)
        mapping[sha] = _write_object(repo, "commit", _join(out, message))

    updates: list[RefUpdate] = []
    for ref in graph.refs:
        commit = ref.commit
        if commit is None or commit not in mapping:
            continue
        if ref.type == "commit":
            new_target = mapping[commit]
        elif ref.peeled_type == "tag":
            continue  # nested tags are left untouched
        else:
            _, body = _read_objects(git, repo, [ref.sha])[ref.sha]
            body = _SIG_RE.sub(b"\n", body)
            headers, message = _split_headers(body)
            rebuilt = []
            for key, value in headers:
                if key == b"object":
                    rebuilt.append((key, mapping[commit].encode()))
                elif key == b"tagger" and _matches(value, identities):
                    rebuilt.append((key, _replace_identity(value, new_name, new_email)))
                else:
                    rebuilt.append((key, value))
            new_target = _write_object(repo, "tag", _join(rebuilt, message))
        if new_target != ref.sha:
            updates.append(RefUpdate(ref.name, ref.sha, new_target))

    lines = [f"update {u.ref} {u.new} {u.old}" for u in updates if u.new]
    if lines:
        git.run_bytes(["update-ref", "--stdin"], cwd=repo, input_bytes=("\n".join(lines) + "\n").encode())

    expected = important(local_refs(git, repo))
    _verify_authors(git, repo, graph, expected, identities)
    return RewriteResult(updates, expected, 0, len(graph.order), matched)


def _verify_authors(git: GitRunner, repo: Path, graph: _Graph, refs_now: dict[str, str],
                    identities: set[tuple[str, str]]) -> None:
    fsck = git.run(["fsck", "--full", "--no-dangling", "--no-progress"], cwd=repo, check=False)
    if fsck.returncode != 0:
        raise GitError("Verification failed: git fsck reported errors: " + fsck.stderr[-300:])
    original = {r.name: r for r in graph.refs}
    for name, sha in refs_now.items():
        if not name.startswith("refs/heads/"):
            continue
        old_tip = original[name].commit
        trees = git.run(["rev-parse", f"{old_tip}^{{tree}}", f"{sha}^{{tree}}"], cwd=repo).stdout.split()
        if trees[0] != trees[1]:
            raise GitError(f"Verification failed: files on {name} changed")
    tips = sorted(set(refs_now.values()))
    reachable = git.run_bytes(["rev-list", "--stdin"], cwd=repo,
                              input_bytes=("\n".join(tips) + "\n").encode()).decode().split() if tips else []
    if len(reachable) != len(graph.order):
        raise GitError(f"Verification failed: expected {len(graph.order)} commits, found {len(reachable)}")
    remaining = count_authored(git, repo, identities)
    if remaining:
        raise GitError(f"Verification failed: {remaining} commit(s) still use the old identity")


# --------------------------------------------------------------------------- rewrite
def _write_object(repo: Path, typ: str, body: bytes) -> str:
    raw = f"{typ} {len(body)}\x00".encode() + body
    sha = hashlib.sha1(raw).hexdigest()  # noqa: S324 - git object id, not security
    path = repo / "objects" / sha[:2] / sha[2:]
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-obj-")
        with os.fdopen(fd, "wb") as fh:
            fh.write(zlib.compress(raw))
        os.chmod(tmp, 0o444)
        os.replace(tmp, path)
    return sha


def _split_headers(body: bytes) -> tuple[list[tuple[bytes, bytes]], bytes]:
    head, sep, message = body.partition(b"\n\n")
    headers: list[tuple[bytes, bytes]] = []
    for line in head.split(b"\n"):
        if line.startswith(b" ") and headers:
            key, value = headers[-1]
            headers[-1] = (key, value + b"\n" + line)
        else:
            key, _, value = line.partition(b" ")
            headers.append((key, value))
    return headers, message if sep else b""


def _join(headers: list[tuple[bytes, bytes]], message: bytes) -> bytes:
    return b"\n".join(k + b" " + v for k, v in headers) + b"\n\n" + message


@dataclass
class RefUpdate:
    ref: str
    old: str | None  # None = ref must not exist yet
    new: str | None  # None = delete


@dataclass
class RewriteResult:
    updates: list[RefUpdate]
    expected_refs: dict[str, str]
    squash_roots: int
    rewritten: int
    removed: int


def rewrite_history(git: GitRunner, repo: Path, cutoff: datetime, tag_policy: str = "snapshot",
                    squash_style: str = "summary") -> RewriteResult:
    """Rewrite refs in a *disposable working mirror* and verify the result locally.

    Must never be called on a backup directory.
    """
    if tag_policy not in TAG_POLICIES:
        raise ValueError("invalid tag policy")
    analysis = analyze_history(git, repo, cutoff, tag_policy, squash_style)
    if analysis.blockers:
        raise GitError("Rewrite blocked: " + "; ".join(analysis.blockers))
    if not analysis.rewrite_required:
        raise GitError("History rewrite is not required for this cutoff.")

    graph = _build_graph(git, repo, cutoff)
    boundaries = _boundaries(graph, tag_policy, squash_style)
    objects = _read_objects(git, repo, sorted(boundaries) + [s for s in graph.order if s not in graph.old])
    day = cutoff.date().isoformat()
    position = {sha: i for i, sha in enumerate(graph.order)}

    mapping: dict[str, str] = {}
    for b in sorted(boundaries, key=position.__getitem__):
        headers, _ = _split_headers(objects[b][1])
        keep = [(k, v) for k, v in headers if k in (b"tree", b"author", b"committer")]
        count = _ancestor_count(graph, b)
        title = "Squashed history before" if squash_style == "summary" else "Snapshot of history before"
        message = (
            f"{title} {day}\n\n"
            f"This commit replaces {count} commit(s) dated before {day}.\n"
            f"Original commit: {b}\n"
        ).encode()
        mapping[b] = _write_object(repo, "commit", _join(keep, message))

    for sha in graph.order:
        if sha in graph.old:
            continue
        headers, message = _split_headers(objects[sha][1])
        new_parents: list[str] = []
        for p in graph.parents[sha]:
            if p in graph.old and squash_style == "none":
                continue  # the parent disappears; this commit's tree already holds every older file
            mapped = mapping[p]
            if mapped not in new_parents:
                new_parents.append(mapped)
        out: list[tuple[bytes, bytes]] = []
        for key, value in headers:
            if key == b"tree":
                out.append((key, value))
                out.extend((b"parent", p.encode()) for p in new_parents)
            elif key in (b"parent", b"gpgsig", b"gpgsig-sha256"):
                continue
            else:
                out.append((key, value))
        mapping[sha] = _write_object(repo, "commit", _join(out, message))

    updates: list[RefUpdate] = []
    for ref in graph.refs:
        commit = ref.commit
        is_branch = ref.name.startswith("refs/heads/")
        if ref.type == "tag" and ref.peeled_type == "tag":
            updates.append(RefUpdate(ref.name, ref.sha, None))
            continue
        if commit is None:
            continue
        if commit not in mapping or (not is_branch and tag_policy == "drop" and commit in graph.old):
            updates.append(RefUpdate(ref.name, ref.sha, None))
            continue
        new_commit = mapping[commit]
        if ref.type == "commit":
            new_target = new_commit
        else:
            typ, body = _read_objects(git, repo, [ref.sha])[ref.sha]
            body = _SIG_RE.sub(b"\n", body)
            headers, message = _split_headers(body)
            headers = [(k, new_commit.encode() if k == b"object" else v) for k, v in headers]
            new_target = _write_object(repo, "tag", _join(headers, message))
        if new_target != ref.sha:
            updates.append(RefUpdate(ref.name, ref.sha, new_target))

    lines = []
    for u in updates:
        if u.new is None:
            lines.append(f"delete {u.ref} {u.old}")
        else:
            lines.append(f"update {u.ref} {u.new} {u.old}")
    if lines:
        git.run_bytes(["update-ref", "--stdin"], cwd=repo, input_bytes=("\n".join(lines) + "\n").encode())

    expected = important(local_refs(git, repo))
    _verify_rewrite(git, repo, graph, boundaries, expected)
    removed = len(graph.old)  # every pre-cutoff commit id is gone (replaced by squash roots)
    return RewriteResult(updates, expected, len(boundaries), len(graph.order) - len(graph.old), removed)


def _verify_rewrite(git: GitRunner, repo: Path, graph: _Graph, boundaries: set[str], refs_now: dict[str, str]) -> None:
    fsck = git.run(["fsck", "--full", "--no-dangling", "--no-progress"], cwd=repo, check=False)
    if fsck.returncode != 0:
        raise GitError("Verification failed: git fsck reported errors: " + fsck.stderr[-300:])

    original = {r.name: r for r in graph.refs}
    for name, sha in refs_now.items():
        if not name.startswith("refs/heads/"):
            continue
        old_tip = original[name].commit
        trees = git.run(["rev-parse", f"{old_tip}^{{tree}}", f"{sha}^{{tree}}"], cwd=repo).stdout.split()
        if trees[0] != trees[1]:
            raise GitError(f"Verification failed: files on {name} changed")
    missing = [n for n in original if n.startswith("refs/heads/") and n not in refs_now]
    if missing:
        raise GitError(f"Verification failed: branches disappeared: {missing}")

    tips = sorted(set(refs_now.values()))
    reachable = set(
        git.run_bytes(["rev-list", "--stdin"], cwd=repo, input_bytes=("\n".join(tips) + "\n").encode())
        .decode().split()
    ) if tips else set()
    leaked = reachable & graph.old
    if leaked:
        raise GitError(f"Verification failed: {len(leaked)} pre-cutoff commit(s) still reachable")
    expected_count = (len(graph.order) - len(graph.old)) + len(boundaries)
    if len(reachable) != expected_count:
        raise GitError(f"Verification failed: expected {expected_count} commits, found {len(reachable)}")


def build_push_args(url: str, updates: list[RefUpdate]) -> list[str]:
    """Atomic push whose leases pin every ref to the SHA seen in the verified backup.

    Refspecs deliberately have no '+' prefix: '+' would force the ref and bypass the lease.
    """
    args = ["push", "--atomic", "--porcelain", "--no-verify"]
    refspecs = []
    for u in updates:
        args.append(f"--force-with-lease={u.ref}:{u.old or ''}")
        refspecs.append(f":{u.ref}" if u.new is None else f"{u.new}:{u.ref}")
    return [*args, "--", url, *refspecs]
