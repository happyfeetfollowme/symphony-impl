from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from symphony_general.approval import ApprovalGate
from symphony_general.audit import AuditLog
from symphony_general.models import ActionProposal


@dataclass(frozen=True)
class ActionExecutionResult:
    proposal_id: str
    executed: bool
    dry_run: bool
    message: str
    output: dict[str, Any]


class ActionExecutor:
    def __init__(self, approval_gate: ApprovalGate, audit_log: AuditLog, dry_run: bool = True) -> None:
        self.approval_gate = approval_gate
        self.audit_log = audit_log
        self.dry_run = dry_run

    def execute(self, proposal: ActionProposal) -> ActionExecutionResult:
        if self.approval_gate.requires_approval(proposal) and not self.approval_gate.is_approved(proposal.id):
            result = ActionExecutionResult(
                proposal_id=proposal.id,
                executed=False,
                dry_run=self.dry_run,
                message="approval required before execution",
                output={"status": "blocked_for_approval"},
            )
            self.audit_log.record("action.blocked", {"proposal": proposal, "result": result})
            return result

        result = ActionExecutionResult(
            proposal_id=proposal.id,
            executed=not self.dry_run,
            dry_run=self.dry_run,
            message="dry run recorded" if self.dry_run else "action executed",
            output={"action_type": proposal.action_type, "target_system": proposal.target_system},
        )
        self.audit_log.record("action.executed", {"proposal": proposal, "result": result})
        return result
