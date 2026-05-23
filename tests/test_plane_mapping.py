from __future__ import annotations

import unittest

from symphony_general.models import RiskLevel
from symphony_general.plane import PlaneClient, PlaneConfig


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


if __name__ == "__main__":
    unittest.main()
