from __future__ import annotations

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
    ) -> None:
        self.task_source = task_source
        self.context_manager = context_manager
        self.runner = runner
        self.approval_gate = approval_gate
        self.action_executor = action_executor
        self.audit_log = audit_log
        self.workflow = workflow or {}

    def poll_once(self) -> PollSummary:
        tasks = self.task_source.list_candidate_tasks()
        results: list[RunResult] = []
        claimed = completed = pending_approval = failed = 0

        for task in tasks:
            try:
                self.audit_log.record("task.claiming", {"task": task})
                self.task_source.claim_task(task)
                claimed += 1
                context = self.context_manager.create_for(task)
                result = self.runner.start_run(task, context, self.workflow)
                results.append(result)
                self.audit_log.record("run.finished", {"result": result})

                if result.error:
                    self.task_source.sync_failure(task, f"Symphony run failed: {result.error}")
                    failed += 1
                    continue

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
                    completed += 1
                    continue

                gated = [proposal for proposal in result.proposals if self.approval_gate.requires_approval(proposal)]
                if gated:
                    for proposal in gated:
                        self.approval_gate.submit(proposal)
                    body = self._approval_comment(result)
                    self.task_source.mark_needs_human(task, body)
                    pending_approval += 1
                    continue

                for proposal in result.proposals:
                    self.action_executor.execute(proposal)
                self.task_source.sync_success(task, self._success_comment(result))
                completed += 1
            except Exception as exc:  # noqa: BLE001 - boundary should convert failures to tracker state.
                failed += 1
                self.audit_log.record("task.failed", {"task": task, "error": str(exc)})
                try:
                    self.task_source.sync_failure(task, f"Symphony orchestration failed: {exc}")
                except Exception as sync_exc:  # noqa: BLE001
                    self.audit_log.record("task.failure_sync_failed", {"task": task, "error": str(sync_exc)})

        return PollSummary(
            candidates=len(tasks),
            claimed=claimed,
            completed=completed,
            pending_approval=pending_approval,
            failed=failed,
            results=tuple(results),
        )

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
