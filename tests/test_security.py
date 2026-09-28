import logging
import subprocess
from types import SimpleNamespace

import pytest
from telegram.ext import ApplicationHandlerStop

from ghbot.git.runner import GitError, GitRunner
from ghbot.logging_setup import RedactingFilter, Redactor

GH_TOKEN = "ghp_" + "A1b2C3d4" * 5
BOT_TOKEN = "7123456789:AAH" + "x" * 32


def test_redactor_removes_secrets_and_token_shapes():
    r = Redactor([GH_TOKEN, BOT_TOKEN])
    text = f"url https://api.telegram.org/bot{BOT_TOKEN}/getMe token={GH_TOKEN} github_pat_{'z' * 30} Authorization: Bearer abc"
    out = r(text)
    assert GH_TOKEN not in out and BOT_TOKEN not in out and "github_pat_z" not in out and "abc" not in out
    assert Redactor()("https://user:secret@example.com/x") == "https://[REDACTED]@example.com/x"


def test_logging_filter_redacts_args_and_exceptions(caplog):
    r = Redactor([GH_TOKEN])
    handler = logging.Handler()
    records = []
    handler.emit = records.append
    handler.addFilter(RedactingFilter(r))
    logger = logging.getLogger("test.redact")
    logger.addHandler(handler)
    try:
        raise RuntimeError(f"boom {GH_TOKEN}")
    except RuntimeError:
        logger.exception("failed with %s", GH_TOKEN)
    logger.removeHandler(handler)
    record = records[0]
    assert GH_TOKEN not in record.getMessage()
    assert GH_TOKEN not in (record.exc_text or "")


def test_git_runner_never_puts_token_in_args_and_redacts_output(tmp_path, monkeypatch):
    runner = GitRunner(token=GH_TOKEN, home=tmp_path / "h", redactor=Redactor())
    captured = {}
    real_run = subprocess.run

    def spy(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["shell"] = kwargs.get("shell")
        captured["env"] = kwargs.get("env")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    subprocess_repo = tmp_path / "r"
    real_run(["git", "init", "-q", str(subprocess_repo)], check=True)
    with pytest.raises(GitError) as err:
        runner.run(["rev-parse", "--verify", GH_TOKEN], cwd=subprocess_repo, auth=True)
    assert GH_TOKEN not in str(err.value)
    assert captured["shell"] is False
    env = captured["env"]
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_CONFIG_GLOBAL"]
    keys = [env[f"GIT_CONFIG_KEY_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))]
    assert "http.https://github.com/.extraheader" in keys  # scoped to github.com only
    assert "file" not in env["GIT_ALLOW_PROTOCOL"]


def test_git_runner_blocks_file_and_ext_transports(tmp_path):
    runner = GitRunner(token=None, home=tmp_path / "h", redactor=Redactor())
    with pytest.raises(GitError):
        runner.run(["ls-remote", "--", str(tmp_path)])
    with pytest.raises(GitError):
        runner.run(["ls-remote", "--", "ext::sh -c touch% /tmp/pwned"])


def test_git_runner_rejects_nul_args(tmp_path):
    runner = GitRunner(token=None, home=tmp_path / "h", redactor=Redactor())
    with pytest.raises(GitError):
        runner.run(["status", "a\x00b"])


async def test_auth_guard_blocks_other_users():
    from ghbot.bot.handlers.common import auth_guard

    services = SimpleNamespace(settings=SimpleNamespace(telegram_allowed_user_id=42))
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"services": services}))

    def update(user_id, chat_type="private"):
        return SimpleNamespace(effective_user=SimpleNamespace(id=user_id), effective_chat=SimpleNamespace(type=chat_type),
                               callback_query=None)

    await auth_guard(update(42), context)  # allowed: no exception
    with pytest.raises(ApplicationHandlerStop):
        await auth_guard(update(7), context)
    with pytest.raises(ApplicationHandlerStop):
        await auth_guard(update(42, "group"), context)
    no_user = SimpleNamespace(effective_user=None, effective_chat=None, callback_query=None)
    with pytest.raises(ApplicationHandlerStop):
        await auth_guard(no_user, context)


def test_no_shell_true_anywhere():
    import pathlib

    for path in pathlib.Path("ghbot").rglob("*.py"):
        text = path.read_text()
        assert "shell=True" not in text, path
        assert "os.system" not in text and "os.popen" not in text, path
