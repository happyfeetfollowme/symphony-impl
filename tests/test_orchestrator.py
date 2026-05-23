from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path

from symphony_general.actions import ActionExecutor
from symphony_general.approval import ApprovalGate
from symphony_general.audit import AuditLog
from symphony_general.context import ExecutionContextManager
from symphony_general.fixtures import FixturePlaneTaskSource
from symphony_general.orchestrator import Orchestrator
from symphony_general.runner import ProposalOnlyRunner


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


if __name__ == "__main__":
    unittest.main()
