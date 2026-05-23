from __future__ import annotations

from symphony_general.audit import AuditLog
from symphony_general.models import (
    ActionProposal,
    ApprovalDecision,
    ApprovalStatus,
    RiskLevel,
)


class ApprovalGate:
    def __init__(
        self,
        audit_log: AuditLog,
        require_for_risks: set[RiskLevel] | None = None,
        require_for_external_actions: bool = True,
    ) -> None:
        self.audit_log = audit_log
        self.require_for_risks = require_for_risks or {RiskLevel.MEDIUM, RiskLevel.HIGH}
        self.require_for_external_actions = require_for_external_actions
        self._decisions: dict[str, ApprovalDecision] = {}

    def requires_approval(self, proposal: ActionProposal) -> bool:
        if proposal.requires_approval:
            return True
        if proposal.risk_level in self.require_for_risks:
            return True
        return self.require_for_external_actions and bool(proposal.target_system)

    def submit(self, proposal: ActionProposal) -> None:
        self.audit_log.record("approval.pending", {"proposal": proposal})

    def decide(self, decision: ApprovalDecision) -> None:
        self._decisions[decision.proposal_id] = decision
        self.audit_log.record("approval.decided", {"decision": decision})

    def decision_for(self, proposal_id: str) -> ApprovalDecision | None:
        return self._decisions.get(proposal_id)

    def is_approved(self, proposal_id: str) -> bool:
        decision = self._decisions.get(proposal_id)
        return bool(decision and decision.status == ApprovalStatus.APPROVED)
