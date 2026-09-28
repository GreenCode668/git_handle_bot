"""Helpers to build real throwaway git repositories for tests."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def git(cwd: Path, *args: str, date: str | None = None, author: tuple[str, str] | None = None) -> str:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(cwd),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    if date:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
    if author:
        env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = author[0]
        env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = author[1]
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


def commit(repo: Path, filename: str, content: str, date: str, message: str | None = None) -> str:
    (repo / filename).write_text(content)
    git(repo, "add", filename)
    git(repo, "commit", "-q", "-m", message or f"edit {filename}", date=date)
    return git(repo, "rev-parse", "HEAD").strip()


def make_sample_repo(root: Path) -> tuple[Path, Path]:
    """Create a work repo plus a bare 'remote' with branches, merges and tags.

    History (dates):
      main:    A(2023-01) - B(2023-06) - C(2024-03) - M(2024-08, merge) - D(2024-09)
      feature:              \\- F1(2023-09) - F2(2024-05) -/
      old-branch: from A, O1(2023-02)
      tags: v0.1 (annotated) -> A, v1.0 (lightweight) -> C, v2.0 (annotated) -> D
    """
    work = root / "work"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    commit(work, "a.txt", "a1", "2023-01-10T10:00:00Z", "A")
    git(work, "tag", "-a", "v0.1", "-m", "first release", date="2023-01-11T00:00:00Z")
    git(work, "branch", "old-branch")
    commit(work, "b.txt", "b1", "2023-06-01T10:00:00Z", "B")
    git(work, "checkout", "-q", "-b", "feature")
    commit(work, "f.txt", "f1", "2023-09-01T10:00:00Z", "F1")
    commit(work, "f.txt", "f2", "2024-05-01T10:00:00Z", "F2")
    git(work, "checkout", "-q", "main")
    commit(work, "c.txt", "c1", "2024-03-01T10:00:00Z", "C")
    git(work, "tag", "v1.0")
    git(work, "merge", "-q", "--no-ff", "feature", "-m", "M", date="2024-08-01T10:00:00Z")
    commit(work, "d.txt", "d1", "2024-09-01T10:00:00Z", "D")
    git(work, "tag", "-a", "v2.0", "-m", "second release", date="2024-09-02T00:00:00Z")
    git(work, "checkout", "-q", "old-branch")
    commit(work, "o.txt", "o1", "2023-02-01T10:00:00Z", "O1")
    git(work, "checkout", "-q", "main")
    remote = root / "remote.git"
    git(root, "clone", "-q", "--mirror", str(work), str(remote))
    return work, remote
