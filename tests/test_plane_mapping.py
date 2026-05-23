from __future__ import annotations

import unittest

from symphony_general.models import RiskLevel
from symphony_general.plane import PlaneClient, PlaneConfig


class FakeWorkspacePlaneClient(PlaneClient):
    def __init__(self) -> None:
        super().__init__(
            PlaneConfig(base_url="https://plane.local", api_key="key", workspace_slug="ws", project_name=None),
            dry_run=False,
        )
        self.patches: list[tuple[str, dict]] = []
        self.posts: list[tuple[str, dict]] = []

    def _get_paginated(self, url: str) -> list[dict]:
        if url.endswith("/projects/"):
            return [{"id": "project-a", "name": "A"}, {"id": "project-b", "name": "B"}]
        if "/projects/project-a/work-items/" in url:
            return []
        if "/projects/project-b/work-items/" in url:
            return [
                {
                    "id": "issue-b",
                    "name": "Workspace task",
                    "description_html": "<p>Do it</p>",
                    "project": "project-b",
                    "state": {"name": "Todo"},
                    "labels": [{"name": "agent-ready"}, {"name": "type:coding"}],
                    "assignees": [{"display_name": "agent-worker"}],
                }
            ]
        if "/projects/project-b/states/" in url:
            return [{"id": "state-progress", "name": "In Progress"}]
        return []

    def _patch(self, url: str, payload: dict):
        self.patches.append((url, payload))
        return {"ok": True}

    def _post(self, url: str, payload: dict):
        self.posts.append((url, payload))
        return {"ok": True}


class PlaneMappingTest(unittest.TestCase):
    def test_maps_plane_work_item_to_task_policy(self) -> None:
        client = PlaneClient(
            PlaneConfig(base_url="https://plane.local", api_key="key", workspace_slug="ws"),
            dry_run=True,
        )
        task = client._task_from_work_item(
            {
                "id": "abc",
                "name": "Send a customer email",
                "description_html": "<p>Hello <strong>world</strong></p>",
                "state": {"name": "Todo"},
                "labels": [{"name": "agent-ready"}, {"name": "risk:high"}, {"name": "type:email"}],
                "assignees": [{"display_name": "agent-worker"}],
            }
        )

        self.assertEqual(task.id, "abc")
        self.assertEqual(task.description, "Hello\nworld")
        self.assertEqual(task.risk_level, RiskLevel.HIGH)
        self.assertTrue(task.requires_approval)
        self.assertEqual(task.target_systems, ("email",))
        self.assertTrue(client._is_candidate(task))

    def test_workspace_wide_polling_uses_task_project_for_updates(self) -> None:
        client = FakeWorkspacePlaneClient()

        tasks = client.list_candidate_tasks()
        client.claim_task(tasks[0])

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].project_id, "project-b")
        self.assertIn("/projects/project-b/work-items/issue-b/", client.patches[0][0])
        self.assertIn("/projects/project-b/work-items/issue-b/comments/", client.posts[0][0])


if __name__ == "__main__":
    unittest.main()
