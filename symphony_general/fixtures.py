from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from symphony_general.models import Task
from symphony_general.plane import PlaneClient, PlaneConfig


class FixturePlaneTaskSource:
    def __init__(self, fixture_path: Path) -> None:
        self.fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        self.client = PlaneClient(
            PlaneConfig(
                base_url="https://plane.fixture",
                api_key="fixture",
                workspace_slug="fixture",
                project_id=self.fixture["project"]["id"],
                project_name=self.fixture["project"]["name"],
            ),
            dry_run=True,
        )
        self.events: list[dict[str, Any]] = []

    def list_candidate_tasks(self) -> list[Task]:
        return [
            task
            for item in self.fixture["work_items"]
            if (task := self.client._task_from_work_item(item))
            and self.client._is_candidate(task)
        ]

    def claim_task(self, task: Task) -> None:
        self.events.append({"type": "claim", "task_id": task.id})

    def mark_needs_human(self, task: Task, body: str) -> None:
        self.events.append({"type": "needs_human", "task_id": task.id, "body": body})

    def sync_success(self, task: Task, body: str) -> None:
        self.events.append({"type": "success", "task_id": task.id, "body": body})

    def sync_done(self, task: Task, body: str) -> None:
        self.events.append({"type": "done", "task_id": task.id, "body": body})

    def sync_failure(self, task: Task, body: str) -> None:
        self.events.append({"type": "failure", "task_id": task.id, "body": body})
