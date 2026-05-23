from __future__ import annotations

import re
from pathlib import Path

from symphony_general.audit import AuditLog
from symphony_general.models import ExecutionContext, Task


class ExecutionContextManager:
    def __init__(self, root: Path, audit_log: AuditLog) -> None:
        self.root = root
        self.audit_log = audit_log

    def create_for(self, task: Task) -> ExecutionContext:
        task_dir = self.root / self._safe_task_id(task.id)
        task_dir.mkdir(parents=True, exist_ok=True)
        context = ExecutionContext(task_id=task.id, path=task_dir)
        self.audit_log.record("context.ready", {"task_id": task.id, "path": task_dir})
        return context

    def _safe_task_id(self, task_id: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]+", "-", task_id).strip("-") or "task"
