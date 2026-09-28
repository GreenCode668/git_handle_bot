"""Mirror clones, bundles and backup verification (synchronous, filesystem only)."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ghbot.git.runner import GitError, GitRunner

IMPORTANT_PREFIXES = ("refs/heads/", "refs/tags/")


def important(refs: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in refs.items() if k.startswith(IMPORTANT_PREFIXES)}


def ls_remote(git: GitRunner, url: str, *, auth: bool = True) -> dict[str, str]:
    """Return {refname: sha} for the remote, excluding peeled tag entries."""
    out = git.run(["ls-remote", "--", url], auth=auth).stdout
    refs: dict[str, str] = {}
    for line in out.splitlines():
        sha, _, name = line.partition("\t")
        if name and not name.endswith("^{}") and name != "HEAD":
            refs[name] = sha
    return refs


def remote_head(git: GitRunner, url: str, *, auth: bool = True) -> str | None:
    out = git.run(["ls-remote", "--symref", "--", url, "HEAD"], auth=auth).stdout
    for line in out.splitlines():
        if line.startswith("ref: ") and line.endswith("\tHEAD"):
            return line[5:].split("\t")[0]
    return None


def local_refs(git: GitRunner, repo_dir: Path) -> dict[str, str]:
    out = git.run(["for-each-ref", "--format=%(objectname) %(refname)"], cwd=repo_dir).stdout
    return {name: sha for sha, _, name in (line.partition(" ") for line in out.splitlines()) if name}


def object_format(git: GitRunner, repo_dir: Path) -> str:
    result = git.run(["rev-parse", "--show-object-format"], cwd=repo_dir, check=False)
    return result.stdout.strip() or "sha1"


def commit_count(git: GitRunner, repo_dir: Path) -> int:
    refs = important(local_refs(git, repo_dir))
    if not refs:
        return 0
    return int(git.run(["rev-list", "--count", "--branches", "--tags"], cwd=repo_dir).stdout.strip() or 0)


def mirror_clone(git: GitRunner, url: str, dest: Path, *, auth: bool = True, local: bool = False) -> None:
    if dest.exists():
        raise GitError(f"destination already exists: {dest.name}")
    git.run(["clone", "--mirror", "--no-hardlinks", "--", url, str(dest)], auth=auth, local=local)


def dir_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class BackupArtifacts:
    refs: dict[str, str]
    head: str | None
    commit_count: int
    bundle_sha256: str | None
    size_bytes: int
    object_format: str
    wiki_included: bool
    warnings: list[str] = field(default_factory=list)


def create_backup_artifacts(git: GitRunner, url: str, dest: Path, *, auth: bool = True,
                            wiki_url: str | None = None) -> BackupArtifacts:
    """Mirror-clone `url` into dest/repo.git and write dest/repo.bundle.

    Raises GitError if the clone does not match what the remote advertised.
    """
    dest.mkdir(parents=True, exist_ok=False)
    warnings: list[str] = []
    advertised_before = important(ls_remote(git, url, auth=auth))
    mirror = dest / "repo.git"
    mirror_clone(git, url, mirror, auth=auth)
    refs = local_refs(git, mirror)
    if important(refs) != advertised_before:
        # A push may have raced the clone; the clone itself is consistent, so we
        # compare against a fresh listing instead of failing blindly.
        advertised_after = important(ls_remote(git, url, auth=auth))
        if important(refs) != advertised_after:
            raise GitError("Mirror clone refs do not match the remote repository (concurrent pushes?)")
        warnings.append("Remote changed during backup; backup reflects the newer state.")

    head = None
    head_file = mirror / "HEAD"
    if head_file.is_file():
        text = head_file.read_text().strip()
        head = text[5:] if text.startswith("ref: ") else None

    bundle_sha = None
    if important(refs):
        bundle = dest / "repo.bundle"
        git.run(["bundle", "create", str(bundle), "--branches", "--tags"], cwd=mirror)
        bundle_sha = sha256_file(bundle)
    else:
        warnings.append("Repository has no branches or tags (empty repository).")

    wiki_included = False
    if wiki_url:
        try:
            mirror_clone(git, wiki_url, dest / "wiki.git", auth=auth)
            wiki_included = True
        except GitError:
            shutil.rmtree(dest / "wiki.git", ignore_errors=True)

    lfs = _uses_lfs(git, mirror, refs)
    if lfs:
        warnings.append("Git LFS pointers detected: LFS file contents are NOT included in this backup.")

    return BackupArtifacts(
        refs=refs,
        head=head,
        commit_count=commit_count(git, mirror),
        bundle_sha256=bundle_sha,
        size_bytes=dir_size(dest),
        object_format=object_format(git, mirror),
        wiki_included=wiki_included,
        warnings=warnings,
    )


def _uses_lfs(git: GitRunner, repo_dir: Path, refs: dict[str, str]) -> bool:
    for ref in important(refs):
        if not ref.startswith("refs/heads/"):
            continue
        result = git.run(["cat-file", "-p", f"{ref}:.gitattributes"], cwd=repo_dir, check=False)
        if result.returncode == 0 and "filter=lfs" in result.stdout:
            return True
    return False


@dataclass
class VerifyReport:
    ok: bool
    checks: list[tuple[str, bool, str]]

    def failed(self) -> list[str]:
        return [f"{name}: {detail}" for name, passed, detail in self.checks if not passed]


def verify_backup_artifacts(git: GitRunner, backup_dir: Path, scratch_root: Path) -> VerifyReport:
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, passed: bool, detail: str = "") -> bool:
        checks.append((name, passed, "" if passed else detail))
        return passed

    manifest_path = backup_dir / "manifest.json"
    mirror = backup_dir / "repo.git"
    if not check("manifest present", manifest_path.is_file(), "manifest.json missing"):
        return VerifyReport(False, checks)
    try:
        manifest: dict[str, Any] = json.loads(manifest_path.read_text())
        expected_refs: dict[str, str] = manifest["refs"]
    except (ValueError, KeyError) as exc:
        check("manifest readable", False, str(exc))
        return VerifyReport(False, checks)
    if not check("mirror present", (mirror / "HEAD").is_file(), "repo.git missing"):
        return VerifyReport(False, checks)

    fsck = git.run(["fsck", "--full", "--no-dangling", "--no-progress"], cwd=mirror, check=False)
    check("git fsck", fsck.returncode == 0, fsck.stderr.strip()[-300:])

    actual = local_refs(git, mirror)
    check("refs match manifest", actual == expected_refs,
          f"{len(set(actual.items()) ^ set(expected_refs.items()))} ref differences")

    count = commit_count(git, mirror)
    check("commit count", count == manifest.get("commit_count"), f"{count} != {manifest.get('commit_count')}")

    expected_important = important(expected_refs)
    bundle = backup_dir / "repo.bundle"
    if expected_important:
        if check("bundle present", bundle.is_file(), "repo.bundle missing"):
            check("bundle checksum", sha256_file(bundle) == manifest.get("bundle_sha256"), "sha256 mismatch")
            scratch_root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=scratch_root, prefix="verify-") as tmp:
                probe = Path(tmp) / "probe.git"
                git.run(["init", "--bare", "--quiet", str(probe)])
                git.run(["bundle", "verify", "--quiet", str(bundle)], cwd=probe)
                git.run(["fetch", "--quiet", "--no-tags", str(bundle), "+refs/*:refs/*"], cwd=probe, local=True)
                restored = local_refs(git, probe)
                check("bundle restores all branches/tags", restored == expected_important,
                      f"{len(set(restored.items()) ^ set(expected_important.items()))} ref differences")
                probe_fsck = git.run(["fsck", "--full", "--no-dangling", "--no-progress"], cwd=probe, check=False)
                check("restored bundle fsck", probe_fsck.returncode == 0, probe_fsck.stderr.strip()[-300:])
    else:
        check("empty repository", True, "no refs to bundle")

    return VerifyReport(all(passed for _, passed, _ in checks), checks)
