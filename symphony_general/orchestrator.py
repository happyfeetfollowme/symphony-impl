from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Protocol

from symphony_general.actions import ActionExecutor
from symphony_general.approval import ApprovalGate
from symphony_general.audit import AuditLog
from symphony_general.context import ExecutionContextManager
from symphony_general.models import ApprovalDecision, ApprovalStatus, RunResult, Task
from symphony_general.runner import AgentRunner


class TaskSource(Protocol):
    def list_candidate_tasks(self) -> list[Task]:
        ...

    def claim_task(self, task: Task) -> None:
        ...

    def mark_needs_human(self, task: Task, body: str) -> None:
        ...

    def sync_success(self, task: Task, body: str) -> None:
        ...

    def sync_done(self, task: Task, body: str) -> None:
        ...

    def sync_failure(self, task: Task, body: str) -> None:
        ...


@dataclass(frozen=True)
class PollSummary:
    candidates: int
    claimed: int
    completed: int
    pending_approval: int
    failed: int
    results: tuple[RunResult, ...]


@dataclass(frozen=True)
class _RunningTask:
    task: Task
    future: Future[RunResult]


@dataclass
class _PollStats:
    completed: int = 0
    pending_approval: int = 0
    failed: int = 0
    results: list[RunResult] | None = None

    def __post_init__(self) -> None:
        if self.results is None:
            self.results = []

    def merge(self, other: "_PollStats") -> None:
        self.completed += other.completed
        self.pending_approval += other.pending_approval
        self.failed += other.failed
        self.results.extend(other.results or [])


class Orchestrator:
    def __init__(
        self,
        task_source: TaskSource,
        context_manager: ExecutionContextManager,
        runner: AgentRunner,
        approval_gate: ApprovalGate,
        action_executor: ActionExecutor,
        audit_log: AuditLog,
        workflow: dict[str, object] | None = None,
        max_concurrency: int = 1,
    ) -> None:
        self.task_source = task_source
        self.context_manager = context_manager
        self.runner = runner
        self.approval_gate = approval_gate
        self.action_executor = action_executor
        self.audit_log = audit_log
        self.workflow = workflow or {}
        self.max_concurrency = max(1, max_concurrency)
        self._executor = ThreadPoolExecutor(max_workers=self.max_concurrency)
        self._running: dict[str, _RunningTask] = {}

    def poll_once(self, wait_for_completion: bool = True) -> PollSummary:
        stats = self._reap_finished(wait_for_completion=False)
        tasks = self.task_source.list_candidate_tasks()
        claimed = 0

        for task in tasks:
            if task.id in self._running:
                continue
            if self._available_slots() <= 0 and wait_for_completion:
                stats.merge(self._reap_finished(wait_for_completion=True))
            if self._available_slots() <= 0:
                break
            claimed += self._dispatch_task(task)

        if wait_for_completion:
            stats.merge(self._reap_finished(wait_for_completion=True))

        return PollSummary(
            candidates=len(tasks),
            claimed=claimed,
            completed=stats.completed,
            pending_approval=stats.pending_approval,
            failed=stats.failed,
            results=tuple(stats.results or []),
        )

    def shutdown(self, wait_for_running: bool = True) -> None:
        self._executor.shutdown(wait=wait_for_running, cancel_futures=False)

    def drain_running(self) -> PollSummary:
        stats = self._reap_finished(wait_for_completion=True)
        return PollSummary(
            candidates=0,
            claimed=0,
            completed=stats.completed,
            pending_approval=stats.pending_approval,
            failed=stats.failed,
            results=tuple(stats.results or []),
        )

    def _dispatch_task(self, task: Task) -> int:
        try:
            self.audit_log.record("task.claiming", {"task": task})
            self.task_source.claim_task(task)
            context = self.context_manager.create_for(task)
            future = self._executor.submit(self.runner.start_run, task, context, self.workflow)
            self._running[task.id] = _RunningTask(task=task, future=future)
            self.audit_log.record(
                "run.dispatched",
                {
                    "task_id": task.id,
                    "max_concurrency": self.max_concurrency,
                    "running": len(self._running),
                },
            )
            return 1
        except Exception as exc:  # noqa: BLE001 - boundary should convert failures to tracker state.
            self._record_orchestration_failure(task, exc)
            return 0

    def _reap_finished(self, wait_for_completion: bool) -> _PollStats:
        stats = _PollStats()
        while self._running:
            futures = [running.future for running in self._running.values()]
            timeout = None if wait_for_completion else 0
            done, _pending = wait(futures, timeout=timeout, return_when=FIRST_COMPLETED)
            if not done:
                break
            for future in done:
                task_id = self._task_id_for_future(future)
                if not task_id:
                    continue
                running = self._running.pop(task_id)
                stats.merge(self._finalize_run(running.task, future))
            if not wait_for_completion:
                break
        return stats

    def _task_id_for_future(self, future: Future[RunResult]) -> str | None:
        for task_id, running in self._running.items():
            if running.future is future:
                return task_id
        return None

    def _finalize_run(self, task: Task, future: Future[RunResult]) -> _PollStats:
        stats = _PollStats()
        try:
            result = future.result()
            stats.results.append(result)
            self.audit_log.record("run.finished", {"result": result})
            self._sync_result(task, result, stats)
        except Exception as exc:  # noqa: BLE001 - boundary should convert failures to tracker state.
            self._record_orchestration_failure(task, exc)
            stats.failed += 1
        return stats

    def _sync_result(self, task: Task, result: RunResult, stats: _PollStats) -> None:
        if result.error:
            self.task_source.sync_failure(task, f"Symphony run failed: {result.error}")
            stats.failed += 1
            return

        if self._is_human_approved(task):
            for proposal in result.proposals:
                self.approval_gate.decide(
                    ApprovalDecision(
                        proposal_id=proposal.id,
                        status=ApprovalStatus.APPROVED,
                        approver="plane:Human Approved",
                        rationale=f"Plane state was {task.state}",
                    )
                )
                self.action_executor.execute(proposal)
            self.task_source.sync_done(task, self._done_comment(result))
            stats.completed += 1
            return

        gated = [proposal for proposal in result.proposals if self.approval_gate.requires_approval(proposal)]
        if gated:
            for proposal in gated:
                self.approval_gate.submit(proposal)
            self.task_source.mark_needs_human(task, self._approval_comment(result))
            stats.pending_approval += 1
            return

        for proposal in result.proposals:
            self.action_executor.execute(proposal)
        self.task_source.sync_success(task, self._success_comment(result))
        stats.completed += 1

    def _record_orchestration_failure(self, task: Task, exc: Exception) -> None:
        self.audit_log.record("task.failed", {"task": task, "error": str(exc)})
        try:
            self.task_source.sync_failure(task, f"Symphony orchestration failed: {exc}")
        except Exception as sync_exc:  # noqa: BLE001
            self.audit_log.record("task.failure_sync_failed", {"task": task, "error": str(sync_exc)})

    def _available_slots(self) -> int:
        return max(self.max_concurrency - len(self._running), 0)

    def _approval_comment(self, result: RunResult) -> str:
        proposal_lines = "\n".join(
            f"- {proposal.id}: {proposal.summary} ({proposal.action_type})"
            for proposal in result.proposals
        )
        return (
            "Symphony prepared a proposal that requires human approval before execution.\n\n"
            f"Run: {result.run_id}\n"
            f"Summary: {result.summary}\n"
            f"Proposals:\n{proposal_lines}"
        )

    def _success_comment(self, result: RunResult) -> str:
        return f"Symphony completed run {result.run_id}.\n\n{result.summary}"

    def _done_comment(self, result: RunResult) -> str:
        return (
            f"Symphony completed approved run {result.run_id}.\n\n"
            f"{result.summary}\n\n"
            "The approved proposal path was executed and the work item was moved to Done."
        )

    def _is_human_approved(self, task: Task) -> bool:
        approved_state = str(self.workflow.get("human_approved_state", "Human Approved"))
        return task.state.casefold() == approved_state.casefold()
