from __future__ import annotations

from pathlib import Path

import pytest

from ghbot.git.runner import GitRunner
from ghbot.logging_setup import Redactor


@pytest.fixture
def git_runner(tmp_path: Path) -> GitRunner:
    # Tests use local bare repositories, so the file transport is allowed here only.
    return GitRunner(token=None, home=tmp_path / "home", allowed_protocols="https:file", redactor=Redactor())
