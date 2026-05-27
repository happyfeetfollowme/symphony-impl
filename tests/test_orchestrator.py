from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path
from threading import Event, Lock

from symphony_general.actions import ActionExecutor
from symphony_general.approval import ApprovalGate
from symphony_general.audit import AuditLog
from symphony_general.context import ExecutionContextManager
from symphony_general.fixtures import FixturePlaneTaskSource
from symphony_general.orchestrator import Orchestrator
from symphony_general.models import ExecutionContext, RiskLevel, RunResult, Task
from symphony_general.runner import AgentRunner, ProposalOnlyRunner


class MemoryTaskSource:
    def __init__(self, tasks: list[Task]) -> None:
        self.tasks = tasks
        self.events: list[dict[str, str]] = []

    def list_candidate_tasks(self) -> list[Task]:
        return self.tasks

    def claim_task(self, task: Task) -> None:
        self.events.append({"type": "claim", "task_id": task.id})

    def mark_needs_human(self, task: Task, body: str) -> None:
        self.events.append({"type": "needs_human", "task_id": task.id})

    def sync_success(self, task: Task, body: str) -> None:
        self.events.append({"type": "success", "task_id": task.id})

    def sync_done(self, task: Task, body: str) -> None:
        self.events.append({"type": "done", "task_id": task.id})

    def sync_failure(self, task: Task, body: str) -> None:
        self.events.append({"type": "failure", "task_id": task.id})


class BlockingSuccessRunner(AgentRunner):
    def __init__(self, expected_concurrent: int) -> None:
        self.expected_concurrent = expected_concurrent
        self.active = 0
        self.max_active = 0
        self.lock = Lock()
        self.all_started = Event()

    def start_run(self, task: Task, context: ExecutionContext, workflow: dict[str, object]) -> RunResult:
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if self.max_active >= self.expected_concurrent:
                self.all_started.set()

        self.all_started.wait(timeout=1)

        with self.lock:
            self.active -= 1

        return RunResult(
            task_id=task.id,
            run_id=f"run-{task.id}",
            summary=f"completed {task.id}",
            completed=True,
        )

    def cancel_run(self, run_id: str) -> None:
        return None


class OrchestratorTest(unittest.TestCase):
    def test_medium_risk_fixture_moves_to_needs_human(self) -> None:
        fixture = Path(__file__).resolve().parents[1] / "examples" / "plane_test_project_fixture.json"
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            audit = AuditLog(root / "audit.jsonl")
            approval = ApprovalGate(audit)
            source = FixturePlaneTaskSource(fixture)
            orchestrator = Orchestrator(
                task_source=source,
                context_manager=ExecutionContextManager(root / "workspaces", audit),
                runner=ProposalOnlyRunner(),
                approval_gate=approval,
                action_executor=ActionExecutor(approval, audit, dry_run=True),
                audit_log=audit,
            )

            summary = orchestrator.poll_once()

            self.assertEqual(summary.candidates, 1)
            self.assertEqual(summary.claimed, 1)
            self.assertEqual(summary.pending_approval, 1)
            self.assertEqual(summary.completed, 0)
            self.assertEqual(source.events[0]["type"], "claim")
            self.assertEqual(source.events[1]["type"], "needs_human")
            self.assertTrue((root / "workspaces" / "issue-1" / "task.md").exists())
            self.assertIn("approval.pending", (root / "audit.jsonl").read_text(encoding="utf-8"))

    def test_human_approved_fixture_executes_and_moves_to_done(self) -> None:
        source_fixture = Path(__file__).resolve().parents[1] / "examples" / "plane_test_project_fixture.json"
        fixture_data = json.loads(source_fixture.read_text(encoding="utf-8"))
        fixture_data["work_items"][0]["state"] = {"id": "state-human-approved", "name": "Human Approved"}

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixture = root / "fixture.json"
            fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
            audit = AuditLog(root / "audit.jsonl")
            approval = ApprovalGate(audit)
            source = FixturePlaneTaskSource(fixture)
            orchestrator = Orchestrator(
                task_source=source,
                context_manager=ExecutionContextManager(root / "workspaces", audit),
                runner=ProposalOnlyRunner(),
                approval_gate=approval,
                action_executor=ActionExecutor(approval, audit, dry_run=False),
                audit_log=audit,
                workflow={"human_approved_state": "Human Approved"},
            )

            summary = orchestrator.poll_once()
            audit_text = (root / "audit.jsonl").read_text(encoding="utf-8")

            self.assertEqual(summary.candidates, 1)
            self.assertEqual(summary.claimed, 1)
            self.assertEqual(summary.completed, 1)
            self.assertEqual(summary.pending_approval, 0)
            self.assertEqual(source.events[-1]["type"], "done")
            self.assertIn("approval.decided", audit_text)
            self.assertIn("action.executed", audit_text)

    def test_poll_once_runs_tasks_concurrently_and_orchestrator_syncs_results(self) -> None:
        tasks = [
            Task(
                id="issue-a",
                title="Task A",
                description="Do A",
                source="test",
                state="Todo",
                task_type="coding",
                risk_level=RiskLevel.LOW,
                labels=("agent-ready",),
            ),
            Task(
                id="issue-b",
                title="Task B",
                description="Do B",
                source="test",
                state="Todo",
                task_type="coding",
                risk_level=RiskLevel.LOW,
                labels=("agent-ready",),
            ),
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            audit = AuditLog(root / "audit.jsonl")
            approval = ApprovalGate(audit)
            source = MemoryTaskSource(tasks)
            runner = BlockingSuccessRunner(expected_concurrent=2)
            orchestrator = Orchestrator(
                task_source=source,
                context_manager=ExecutionContextManager(root / "workspaces", audit),
                runner=runner,
                approval_gate=approval,
                action_executor=ActionExecutor(approval, audit, dry_run=True),
                audit_log=audit,
                max_concurrency=2,
            )

            summary = orchestrator.poll_once()
            orchestrator.shutdown()

            self.assertEqual(summary.candidates, 2)
            self.assertEqual(summary.claimed, 2)
            self.assertEqual(summary.completed, 2)
            self.assertEqual(runner.max_active, 2)
            self.assertEqual(
                [event["type"] for event in source.events],
                ["claim", "claim", "success", "success"],
            )


if __name__ == "__main__":
    unittest.main()
