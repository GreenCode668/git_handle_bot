"""The mandatory safety flow for operations that modify GitHub.

    Analyze -> show impact -> ask -> create backup -> verify backup
            -> ask final confirmation -> execute -> verify result -> report

Every stage change is an atomic compare-and-set in SQLite, so a repeated
button press or message can never run a step twice. The repository lock is
held from approval until the operation finishes, is cancelled or expires.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any, ClassVar

from ghbot.db import Operation, RepoLockedError, utcnow
from ghbot.logging_setup import get_redactor
from ghbot.services.backups import BackupError
from ghbot.services.policy import PolicyError
from ghbot.validators import RepoRef, confirmation_phrase, matches_confirmation

if TYPE_CHECKING:
    from ghbot.services.container import Services

log = logging.getLogger(__name__)

Progress = Callable[[str], Awaitable[None]]


class Stage:
    ANALYZED = "analyzed"
    BACKING_UP = "backing_up"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    EXECUTING = "executing"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    INTERRUPTED = "interrupted"


# A verified backup this recent is reused instead of taking an identical one again.
BACKUP_REUSE_WINDOW = timedelta(hours=24)

WAITING = (Stage.ANALYZED, Stage.AWAITING_CONFIRMATION)
RUNNING = (Stage.BACKING_UP, Stage.EXECUTING)
TERMINAL = (Stage.DONE, Stage.FAILED, Stage.CANCELLED, Stage.EXPIRED, Stage.INTERRUPTED)


class SafetyError(Exception):
    """A safety rule stopped the operation. The message is safe to show."""


@dataclass
class Impact:
    title: str
    lines: list[str]
    warnings: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    target_exists: bool = True
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class OperationSpec(ABC):
    kind: ClassVar[str]
    verb: ClassVar[str]  # used in the confirmation phrase, e.g. DELETE owner/repo
    label: ClassVar[str]
    requires_backup: ClassVar[bool] = True  # backup of the *target* when it exists
    confirm_mode: ClassVar[str] = "phrase"  # "phrase" or "button"
    github_write: ClassVar[bool] = True  # subject to the public-only repository policy
    may_create: ClassVar[bool] = False  # may create the target repository when it does not exist

    def __init__(self, services: Services) -> None:
        self.s = services

    @abstractmethod
    async def analyze(self, repo: RepoRef, params: dict[str, Any], progress: Progress) -> Impact: ...

    async def preflight(self, op: Operation, repo: RepoRef) -> None:  # noqa: B027
        """Re-check conditions immediately before execution. Raise SafetyError to stop."""

    async def creation_private(self, repo: RepoRef, params: dict[str, Any]) -> bool:
        """Visibility of a repository this operation would create. Defaults to the refused value."""
        return True

    @abstractmethod
    async def execute(self, op: Operation, repo: RepoRef, progress: Progress) -> dict[str, Any]: ...

    @abstractmethod
    async def verify(self, op: Operation, repo: RepoRef, result: dict[str, Any]) -> list[str]:
        """Return a list of problems; empty means the result was verified."""


async def _noop(_: str) -> None:
    return None


class SafetyEngine:
    def __init__(self, services: Services, specs: list[type[OperationSpec]]) -> None:
        self.s = services
        self.specs: dict[str, OperationSpec] = {spec.kind: spec(services) for spec in specs}

    def spec(self, kind: str) -> OperationSpec:
        if kind not in self.specs:
            raise SafetyError(f"Unknown operation {kind}")
        return self.specs[kind]

    def _repo(self, op: Operation) -> RepoRef:
        owner, _, name = (op.repo or "").partition("/")
        return RepoRef(owner, name)

    async def _ttl(self) -> timedelta:
        minutes = await self.s.db.get_setting("confirmation_ttl_minutes", 30)
        return timedelta(minutes=int(minutes))

    # ---------------------------------------------------------------- stages
    async def start(self, kind: str, repo: RepoRef, params: dict[str, Any], progress: Progress = _noop) -> Operation:
        spec = self.spec(kind)
        await self._policy(spec, repo, params)
        impact = await spec.analyze(repo, params, progress)
        phrase = confirmation_phrase(spec.verb, repo.full_name) if spec.confirm_mode == "phrase" else None
        stage = Stage.CANCELLED if impact.blockers else Stage.ANALYZED
        op = await self.s.db.create_operation(
            kind, repo.full_name, stage, params=params, impact=impact.to_dict(),
            confirm_phrase=phrase, ttl=await self._ttl(),
        )
        if impact.blockers:
            await self.s.db.transition(op.id, [stage], stage, error="blocked: " + "; ".join(impact.blockers))
        return (await self.s.db.get_operation(op.id)) or op

    async def update_params(self, op_id: int, **changes: Any) -> Operation:
        op = await self._get_waiting(op_id, Stage.ANALYZED)
        params = {**op.params, **changes}
        if not await self.s.db.transition(op.id, [Stage.ANALYZED], Stage.ANALYZED, params=params):
            raise SafetyError("Operation changed state; please start again.")
        return await self._get(op_id)

    async def approve(self, op_id: int, progress: Progress = _noop) -> Operation:
        """User accepted the impact: lock the repo, then create and verify the backup."""
        op = await self._get_waiting(op_id, Stage.ANALYZED)
        spec = self.spec(op.kind)
        repo = self._repo(op)
        await self._policy(spec, repo, op.params)
        try:
            await self.s.db.acquire_lock(repo.key, f"{spec.label} #{op.id}", op.id)
        except RepoLockedError as exc:
            raise SafetyError(str(exc)) from None

        if not await self.s.db.transition(op.id, [Stage.ANALYZED], Stage.BACKING_UP):
            await self.s.db.release_lock(repo.key, op.id)
            raise SafetyError("Operation already approved or no longer pending.")

        backup_id = None
        if self._needs_backup(op):
            try:
                backup_id = await self._reusable_backup(repo, progress)
                if backup_id is None:
                    await progress("💾 Creating full mirror backup…")
                    record = await self.s.backups.create(repo, f"before {spec.label} (op #{op.id})", op.id)
                    backup_id = record.id
                    await progress(f"🔎 Verifying backup {record.id}…")
                    report = await self.s.backups.verify(record.id)
                    if not report.ok:
                        raise BackupError(f"Backup {record.id} failed verification: " + "; ".join(report.failed()))
            except BackupError as exc:
                await self._finish(op, Stage.FAILED, error=f"Stopped before any change: {exc}", backup_id=backup_id)
                raise SafetyError(f"🛑 STOPPED. Backup failed, GitHub was not modified.\n{exc}") from None

        await self.s.db.transition(
            op.id, [Stage.BACKING_UP], Stage.AWAITING_CONFIRMATION,
            backup_id=backup_id, expires_at=utcnow() + await self._ttl(),
        )
        return await self._get(op.id)

    async def confirm_phrase(self, text: str, progress: Progress = _noop) -> Operation | None:
        """Handle a typed confirmation. Returns None if the text is not a pending phrase."""
        op = await self.s.db.find_awaiting_confirmation(text.strip(), Stage.AWAITING_CONFIRMATION)
        if op is None:
            return None
        spec = self.spec(op.kind)
        if spec.confirm_mode != "phrase" or not op.confirm_phrase or not matches_confirmation(text, op.confirm_phrase):
            return None
        return await self._execute(op, progress)

    async def confirm_button(self, op_id: int, progress: Progress = _noop) -> Operation:
        op = await self._get(op_id)
        spec = self.spec(op.kind)
        if spec.confirm_mode != "button":
            raise SafetyError("This operation requires typing the confirmation phrase.")
        if op.stage == Stage.ANALYZED:
            op = await self.approve(op_id, progress)
        if op.stage != Stage.AWAITING_CONFIRMATION:
            raise SafetyError("Operation is no longer awaiting confirmation.")
        return await self._execute(op, progress)

    async def cancel(self, op_id: int) -> bool:
        op = await self.s.db.get_operation(op_id)
        if op is None or op.stage not in WAITING:
            return False
        ok = await self.s.db.transition(op.id, list(WAITING), Stage.CANCELLED, error="cancelled by user")
        if ok:
            await self.s.db.release_lock(self._repo(op).key, op.id)
        return ok

    async def cancel_all_waiting(self) -> int:
        count = 0
        for op in await self.s.db.operations_in_stages(list(WAITING)):
            count += int(await self.cancel(op.id))
        return count

    async def expire_stale(self) -> list[Operation]:
        expired = []
        for op in await self.s.db.operations_in_stages(list(WAITING)):
            if op.expired and await self.s.db.transition(op.id, list(WAITING), Stage.EXPIRED, error="confirmation timed out"):
                await self.s.db.release_lock(self._repo(op).key, op.id)
                expired.append(op)
        return expired

    async def recover_on_startup(self) -> list[Operation]:
        """Operations that were running when the process died are marked interrupted."""
        interrupted = []
        for op in await self.s.db.operations_in_stages(list(RUNNING)):
            await self.s.db.transition(
                op.id, list(RUNNING), Stage.INTERRUPTED,
                error=f"Bot restarted while {op.stage}. Check the repository; backup: {op.backup_id or 'none'}",
            )
            interrupted.append(op)
        await self.s.db.release_all_locks()
        return interrupted

    async def _reusable_backup(self, repo: RepoRef, progress: Progress) -> str | None:
        """Reuse a recent verified backup when GitHub still matches it exactly.

        The safety rule is unchanged: the backup is verified again here, and once more
        immediately before execution, together with a fresh GitHub comparison.
        """
        record = await self.s.db.latest_verified_backup(repo.full_name)
        if record is None or utcnow() - record.created_at > BACKUP_REUSE_WINDOW:
            return None
        matches, _ = await self.s.backups.remote_matches(repo.full_name, record.id)
        if not matches:
            return None
        await progress(f"🔎 Re-verifying recent backup {record.id}…")
        if not (await self.s.backups.verify(record.id)).ok:
            return None
        await progress(f"♻️ Reusing verified backup {record.id} (GitHub is unchanged since it was taken).")
        return record.id

    # ---------------------------------------------------------------- policy
    async def _policy(self, spec: OperationSpec, repo: RepoRef, params: dict[str, Any]) -> None:
        """Public-only / owned-only / not-protected rule for every GitHub write. Uses fresh metadata."""
        if not spec.github_write:
            return
        policy = self.s.policy
        try:
            meta = await policy.target_state(repo)
            if meta is None:
                if spec.may_create:
                    await policy.ensure_creatable(repo, private=await spec.creation_private(repo, params))
                else:
                    await policy.ensure_not_protected(repo)
                return
            await policy.ensure_not_protected(repo)
            policy.check_writable_meta(meta)
        except PolicyError as exc:
            raise SafetyError(str(exc)) from None

    async def dry_run(self, kind: str, repo: RepoRef, params: dict[str, Any], progress: Progress = _noop) -> Operation:
        """Simulate an operation: every check and the analysis run, nothing is locked, backed up or modified."""
        spec = self.spec(kind)
        checks: list[list[Any]] = []
        impact: Impact | None = None
        try:
            await self._policy(spec, repo, params)
            checks.append(["repository policy (owned, public, not protected)", True, ""])
            policy_ok = True
        except SafetyError as exc:
            checks.append(["repository policy (owned, public, not protected)", False, str(exc)])
            policy_ok = False
        locks = {lock["repo_key"]: lock for lock in await self.s.db.list_locks()}
        lock = locks.get(repo.key)
        checks.append(["repository not locked by another operation", lock is None, lock["holder"] if lock else ""])
        if policy_ok:
            try:
                impact = await spec.analyze(repo, params, progress)
                checks.append(["analysis", not impact.blockers, "; ".join(impact.blockers)])
            except Exception as exc:  # noqa: BLE001
                checks.append(["analysis", False, get_redactor()(str(exc))])
        if impact is not None and spec.requires_backup and impact.target_exists:
            latest = await self.s.db.latest_verified_backup(repo.full_name)
            detail = f"latest verified backup: {latest.id}" if latest else "no verified backup exists yet"
            checks.append(["a new full mirror backup would be created and verified first", True, detail])
        confirmation = (
            f"typed phrase: {confirmation_phrase(spec.verb, repo.full_name)}" if spec.confirm_mode == "phrase" else "button"
        )
        checks.append(["final confirmation required", True, confirmation])
        would_proceed = all(ok for _, ok, _ in checks)
        op = await self.s.db.create_operation(
            "dryrun", repo.full_name, Stage.DONE,
            params={"operation": kind, **params},
            impact=impact.to_dict() if impact else {"title": f"Dry run: {spec.label}", "lines": []},
        )
        await self.s.db.transition(op.id, [Stage.DONE], Stage.DONE,
                                   result={"checks": checks, "would_proceed": would_proceed, "simulated": spec.label})
        return await self._get(op.id)

    # --------------------------------------------------------------- helpers
    def _needs_backup(self, op: Operation) -> bool:
        spec = self.spec(op.kind)
        return spec.requires_backup and bool((op.impact or {}).get("target_exists", True))

    async def _execute(self, op: Operation, progress: Progress) -> Operation:
        spec = self.spec(op.kind)
        repo = self._repo(op)
        if op.expired:
            await self._finish(op, Stage.EXPIRED, error="confirmation timed out", from_stages=[Stage.AWAITING_CONFIRMATION])
            raise SafetyError("⌛ This confirmation expired. Nothing was changed; start again.")
        if not await self.s.db.transition(op.id, [Stage.AWAITING_CONFIRMATION], Stage.EXECUTING):
            raise SafetyError("Operation is already running or no longer pending.")
        try:
            try:
                await self._policy(spec, repo, op.params)
            except SafetyError as exc:
                raise SafetyError(f"{exc} Nothing was changed.") from None
            if self._needs_backup(op):
                await self._guarantee_backup(op, repo, progress)
            await spec.preflight(op, repo)
            await progress(f"⚙️ Executing {spec.label}…")
            result = await spec.execute(op, repo, progress)
        except SafetyError as exc:
            await self._finish(op, Stage.FAILED, error=str(exc))
            raise
        except Exception as exc:  # noqa: BLE001
            message = get_redactor()(str(exc))
            log.exception("Operation #%s (%s) failed during execution", op.id, op.kind)
            await self._finish(op, Stage.FAILED, error=f"Execution failed: {message}")
            hint = f" Restore with /restore {op.backup_id}" if op.backup_id else ""
            raise SafetyError(f"❌ {spec.label} failed: {message}.{hint}") from None

        await progress("🔎 Verifying result…")
        try:
            problems = await spec.verify(op, repo, result)
        except Exception as exc:  # noqa: BLE001
            problems = [f"verification error: {get_redactor()(str(exc))}"]
        if problems:
            await self._finish(op, Stage.FAILED, result=result, error="Post-execution verification failed: " + "; ".join(problems))
        else:
            await self._finish(op, Stage.DONE, result=result)
        return await self._get(op.id)

    async def _guarantee_backup(self, op: Operation, repo: RepoRef, progress: Progress) -> None:
        """Most important rule: no verified backup that matches GitHub, no execution."""
        if not op.backup_id:
            raise SafetyError("🛑 No backup is attached to this operation. Nothing was changed.")
        record = await self.s.db.get_backup(op.backup_id)
        if record is None or record.status != "verified":
            raise SafetyError(f"🛑 Backup {op.backup_id} is not verified. Nothing was changed.")
        await progress(f"🔎 Re-verifying backup {op.backup_id} before execution…")
        report = await self.s.backups.verify(op.backup_id)
        if not report.ok:
            raise SafetyError(f"🛑 Backup {op.backup_id} failed re-verification: {'; '.join(report.failed())}. Nothing was changed.")
        matches, detail = await self.s.backups.remote_matches(repo.full_name, op.backup_id)
        if not matches:
            raise SafetyError(
                f"🛑 GitHub changed since backup {op.backup_id} ({detail}). Nothing was changed; start again."
            )

    async def _finish(self, op: Operation, stage: str, *, from_stages: list[str] | None = None, **fields: Any) -> None:
        await self.s.db.transition(op.id, from_stages or [op.stage, Stage.EXECUTING, Stage.BACKING_UP], stage, **fields)
        await self.s.db.release_lock(self._repo(op).key, op.id)

    async def _get(self, op_id: int) -> Operation:
        op = await self.s.db.get_operation(op_id)
        if op is None:
            raise SafetyError("Operation not found.")
        return op

    async def _get_waiting(self, op_id: int, stage: str) -> Operation:
        op = await self._get(op_id)
        if op.stage != stage:
            raise SafetyError(f"Operation #{op.id} is {op.stage}, not {stage}.")
        if op.expired:
            await self._finish(op, Stage.EXPIRED, error="timed out", from_stages=[stage])
            raise SafetyError("⌛ This operation expired. Nothing was changed; start again.")
        return op
