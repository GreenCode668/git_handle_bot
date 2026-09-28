"""Analyze many repositories with one cutoff, then approve the selected ones.

Nothing here weakens the per-repository safety flow: every repository gets its own
SafetyEngine operation, its own verified backup and its own typed confirmation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ghbot.db import utcnow
from ghbot.logging_setup import get_redactor
from ghbot.services.safety import Progress, SafetyError, Stage
from ghbot.validators import RepoRef

if TYPE_CHECKING:
    from ghbot.services.container import Services

NOT_REQUIRED = "No history rewrite is required for this cutoff."


@dataclass
class BatchItem:
    repo: str
    status: str  # "rewrite" | "delete" | "not_needed" | "blocked" | "error"
    op_id: int | None = None
    total: int = 0
    before: int = 0
    removed: int = 0
    kept: int = 0
    detail: str = ""


@dataclass
class ApprovalItem:
    repo: str
    op_id: int
    ok: bool
    backup_id: str | None = None
    phrase: str | None = None
    detail: str = ""


async def _noop(_: str) -> None:
    return None


class BatchHistory:
    def __init__(self, services: Services) -> None:
        self.s = services

    async def analyze(self, repos: list[RepoRef], params: dict[str, Any], progress: Progress = _noop) -> list[BatchItem]:
        items: list[BatchItem] = []
        engine = self.s.safety
        for index, repo in enumerate(repos, 1):
            prefix = f"[{index}/{len(repos)}] {repo.full_name}"

            async def step(text: str, prefix: str = prefix) -> None:
                await progress(f"{prefix}: {text}")

            await step("analyzing (read-only)…")
            try:
                op = await engine.start("rewrite_history", repo, dict(params), step)
            except SafetyError as exc:
                items.append(BatchItem(repo.full_name, "blocked", detail=str(exc)))
                continue
            except Exception as exc:  # noqa: BLE001
                items.append(BatchItem(repo.full_name, "error", detail=get_redactor()(str(exc))[:200]))
                continue
            impact = op.impact or {}
            analysis = impact.get("data", {}).get("analysis") or {}
            item = BatchItem(
                repo.full_name, "rewrite" if op.stage == Stage.ANALYZED else "blocked", op.id,
                total=analysis.get("total_commits", 0), before=analysis.get("dated_before", 0),
                removed=analysis.get("squash_count", 0), kept=analysis.get("rewrite_count", 0),
            )
            blockers = [b for b in (impact.get("blockers") or []) if b != NOT_REQUIRED]
            if op.stage != Stage.ANALYZED and blockers:
                item.status = "blocked"
                item.detail = "; ".join(blockers)
            elif item.removed > 0 and item.kept == 0:
                # Nothing at all would survive the cutoff (this also covers repositories whose whole
                # history is older, where no rewrite is "required"): delete the repository instead of
                # leaving snapshot commits behind.
                item = await self._as_deletion(op, repo, item, step)
            elif op.stage != Stage.ANALYZED:
                item.status = "not_needed"
            items.append(item)

        # Long batches: restart the confirmation window once every repository has been analyzed.
        ttl = await engine._ttl()
        for item in items:
            if item.status in ("rewrite", "delete") and item.op_id:
                await self.s.db.transition(item.op_id, [Stage.ANALYZED], Stage.ANALYZED, expires_at=utcnow() + ttl)
        return items

    async def _as_deletion(self, rewrite_op, repo: RepoRef, item: BatchItem, step: Progress) -> BatchItem:
        engine = self.s.safety
        await engine.cancel(rewrite_op.id)
        await step("no commits would remain: preparing repository deletion…")
        try:
            delete_op = await engine.start("delete_repo", repo, {}, step)
        except SafetyError as exc:
            return BatchItem(item.repo, "blocked", detail=str(exc), total=item.total, before=item.before)
        if delete_op.stage != Stage.ANALYZED:
            blockers = (delete_op.impact or {}).get("blockers") or []
            return BatchItem(item.repo, "blocked", delete_op.id, detail="; ".join(blockers),
                             total=item.total, before=item.before)
        return BatchItem(item.repo, "delete", delete_op.id, total=item.total, before=item.before,
                         removed=item.total, kept=0)

    async def analyze_deletions(self, repos: list[RepoRef], progress: Progress = _noop) -> list[BatchItem]:
        """Prepare a repository deletion per repository (no history analysis needed)."""
        items: list[BatchItem] = []
        engine = self.s.safety
        for index, repo in enumerate(repos, 1):
            prefix = f"[{index}/{len(repos)}] {repo.full_name}"

            async def step(text: str, prefix: str = prefix) -> None:
                await progress(f"{prefix}: {text}")

            await step("checking repository…")
            try:
                op = await engine.start("delete_repo", repo, {}, step)
            except SafetyError as exc:
                items.append(BatchItem(repo.full_name, "blocked", detail=str(exc)))
                continue
            except Exception as exc:  # noqa: BLE001
                items.append(BatchItem(repo.full_name, "error", detail=get_redactor()(str(exc))[:200]))
                continue
            impact = op.impact or {}
            if op.stage != Stage.ANALYZED:
                items.append(BatchItem(repo.full_name, "blocked", op.id,
                                       detail="; ".join(impact.get("blockers") or [])))
                continue
            lines = impact.get("lines") or []
            items.append(BatchItem(repo.full_name, "delete", op.id,
                                   detail=" · ".join(line for line in lines[:4] if line)[:160]))

        ttl = await engine._ttl()
        for item in items:
            if item.status == "delete" and item.op_id:
                await self.s.db.transition(item.op_id, [Stage.ANALYZED], Stage.ANALYZED, expires_at=utcnow() + ttl)
        return items

    async def approve(self, op_ids: list[int], progress: Progress = _noop) -> list[ApprovalItem]:
        """Backup + verify each selected repository (rewrite or delete).

        Each one still needs its own typed confirmation phrase afterwards.
        """
        results: list[ApprovalItem] = []
        engine = self.s.safety
        for index, op_id in enumerate(op_ids, 1):
            op = await self.s.db.get_operation(op_id)
            if op is None or op.kind not in ("rewrite_history", "delete_repo"):
                continue
            prefix = f"[{index}/{len(op_ids)}] {op.repo}"

            async def step(text: str, prefix: str = prefix) -> None:
                await progress(f"{prefix}: {text}")

            try:
                approved = await engine.approve(op_id, step)
            except SafetyError as exc:
                results.append(ApprovalItem(op.repo or "?", op_id, False, detail=str(exc)))
                continue
            results.append(ApprovalItem(op.repo or "?", op_id, True, approved.backup_id, approved.confirm_phrase))
        return results

    async def cancel(self, op_ids: list[int]) -> int:
        return sum([int(await self.s.safety.cancel(op_id)) for op_id in op_ids])
