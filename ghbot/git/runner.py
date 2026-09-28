"""Safe git subprocess execution.

* Arguments are always passed as a list; a shell is never used.
* Global/system git config and credential helpers are disabled.
* The GitHub token is supplied through an in-memory config entry scoped to
  https://github.com/ only, so it is never written to disk, never embedded in
  a remote URL and never sent to other hosts.
* All output is redacted before it is returned or raised.
"""

from __future__ import annotations

import base64
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ghbot.logging_setup import Redactor, get_redactor

log = logging.getLogger(__name__)

# Windows processes need these to work at all: without SYSTEMROOT, Winsock cannot
# resolve hostnames ("Could not resolve host"), and TEMP/TMP are used for scratch files.
_WINDOWS_PASSTHROUGH = ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP", "PATHEXT", "COMSPEC")


def platform_env() -> dict[str, str]:
    """OS variables a minimal subprocess environment must keep (empty outside Windows)."""
    if os.name != "nt":
        return {}
    return {key: os.environ[key] for key in _WINDOWS_PASSTHROUGH if key in os.environ}


class GitError(Exception):
    def __init__(self, message: str, returncode: int | None = None) -> None:
        super().__init__(message)
        self.returncode = returncode


@dataclass
class GitResult:
    returncode: int
    stdout: str
    stderr: str


class GitRunner:
    def __init__(self, *, token: str | None, home: Path, timeout: int = 3600,
                 allowed_protocols: str = "https", redactor: Redactor | None = None,
                 auth_host: str = "https://github.com/") -> None:
        self._token = token
        self._home = home
        self._home.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self._protocols = allowed_protocols
        self._redact = redactor or get_redactor()
        self._auth_host = auth_host
        if token:
            self._redact.add_secret(token)
            self._redact.add_secret(self._basic(token))

    @staticmethod
    def _basic(token: str) -> str:
        return base64.b64encode(f"x-access-token:{token}".encode()).decode()

    def _env(self, auth: bool, local: bool = False) -> dict[str, str]:
        config = {
            "credential.helper": "",
            "core.hooksPath": os.devnull,
            "core.askPass": "",
            "protocol.ext.allow": "never",
            "transfer.fsckObjects": "false",
            "gc.auto": "0",
        }
        if auth and self._token:
            config[f"http.{self._auth_host}.extraheader"] = f"AUTHORIZATION: basic {self._basic(self._token)}"
        env = {
            **platform_env(),
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(self._home),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            # `file` is enabled only for internal paths (backups, work dirs), never user URLs.
            "GIT_ALLOW_PROTOCOL": f"{self._protocols}:file" if local else self._protocols,
            "GCM_INTERACTIVE": "never",
            "GIT_CONFIG_COUNT": str(len(config)),
        }
        for i, (key, value) in enumerate(config.items()):
            env[f"GIT_CONFIG_KEY_{i}"] = key
            env[f"GIT_CONFIG_VALUE_{i}"] = value
        return env

    def run(self, args: list[str], *, cwd: Path | str | None = None, input_bytes: bytes | None = None,
            auth: bool = False, check: bool = True, timeout: int | None = None,
            local: bool = False) -> GitResult:
        for arg in args:
            if not isinstance(arg, str) or "\x00" in arg:
                raise GitError("Invalid git argument")
        cmd = ["git", *args]
        try:
            proc = subprocess.run(  # noqa: S603 - list args, shell=False
                cmd,
                cwd=str(cwd) if cwd else None,
                input=input_bytes,
                capture_output=True,
                env=self._env(auth, local),
                timeout=timeout or self.timeout,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise GitError(f"git {args[0]} timed out") from None
        except FileNotFoundError:
            raise GitError("git executable not found") from None
        stdout = proc.stdout.decode("utf-8", "replace")
        stderr = self._redact(proc.stderr.decode("utf-8", "replace"))
        if check and proc.returncode != 0:
            summary = stderr.strip().splitlines()[-5:]
            raise GitError(f"git {args[0]} failed ({proc.returncode}): " + " | ".join(summary), proc.returncode)
        return GitResult(proc.returncode, stdout, stderr)

    def run_bytes(self, args: list[str], *, cwd: Path | str, input_bytes: bytes | None = None) -> bytes:
        """Run a local (non-network) git command and return raw stdout."""
        proc = subprocess.run(  # noqa: S603
            ["git", *args], cwd=str(cwd), input=input_bytes, capture_output=True,
            env=self._env(False), timeout=self.timeout, shell=False, check=False,
        )
        if proc.returncode != 0:
            raise GitError(f"git {args[0]} failed: " + self._redact(proc.stderr.decode("utf-8", "replace"))[-500:])
        return proc.stdout

    def version(self) -> str:
        return self.run(["--version"]).stdout.strip()
