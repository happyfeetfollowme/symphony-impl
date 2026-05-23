from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from symphony_general.audit import AuditLog
from symphony_general.daemon import DaemonConfig, SymphonyDaemon
from symphony_general.orchestrator import PollSummary


class FakeOrchestrator:
    def __init__(self) -> None:
        self.calls = 0

    def poll_once(self) -> PollSummary:
        self.calls += 1
        return PollSummary(
            candidates=1,
            claimed=1,
            completed=1,
            pending_approval=0,
            failed=0,
            results=(),
        )


class DaemonTest(unittest.TestCase):
    def test_daemon_runs_until_max_cycles_and_logs_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake = FakeOrchestrator()
            output = io.StringIO()
            daemon = SymphonyDaemon(
                orchestrator_factory=lambda: fake,
                audit_log=AuditLog(root / "audit.jsonl"),
                config=DaemonConfig(interval_seconds=0, max_cycles=2),
                output=output,
            )

            exit_code = daemon.run()

            self.assertEqual(exit_code, 0)
            self.assertEqual(fake.calls, 2)

            events = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual([event["event"] for event in events], ["daemon.started", "daemon.cycle", "daemon.cycle", "daemon.stopped"])
            self.assertEqual(events[1]["summary"]["completed"], 1)

            audit_text = (root / "audit.jsonl").read_text(encoding="utf-8")
            self.assertIn("daemon.started", audit_text)
            self.assertIn("daemon.cycle", audit_text)
            self.assertIn("daemon.stopped", audit_text)


if __name__ == "__main__":
    unittest.main()
