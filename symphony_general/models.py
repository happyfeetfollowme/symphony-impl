from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

try:
    from datetime import UTC
except ImportError:  # pragma: no cover - Python < 3.11 compatibility.
    UTC = timezone.utc

try:
    from enum import StrEnum
except ImportError:  # pragma: no cover - Python < 3.11 compatibility.
    class StrEnum(str, Enum):
        pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes_requested"


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    description: str
    source: str
    state: str
    labels: tuple[str, ...] = ()
    assignees: tuple[str, ...] = ()
    project_id: str | None = None
    project_name: str | None = None
    url: str | None = None
    task_type: str = "general"
    risk_level: RiskLevel = RiskLevel.LOW
    requires_approval: bool = False
    required_capabilities: tuple[str, ...] = ()
    target_systems: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionContext:
    task_id: str
    path: Path
    created_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class ActionProposal:
    task_id: str
    summary: str
    action_type: str
    payload: dict[str, Any]
    risk_level: RiskLevel = RiskLevel.MEDIUM
    target_system: str | None = None
    requires_approval: bool = True
    id: str = field(default_factory=lambda: f"proposal-{uuid4().hex[:12]}")
    created_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class ApprovalDecision:
    proposal_id: str
    status: ApprovalStatus
    approver: str
    rationale: str = ""
    decided_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class RunResult:
    task_id: str
    run_id: str
    summary: str
    proposals: tuple[ActionProposal, ...] = ()
    completed: bool = False
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.error is None
