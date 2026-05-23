from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from symphony_general.models import ExecutionContext, RiskLevel, Task
from symphony_general.runner import CodexRunner


class CodexRunnerTest(unittest.TestCase):
    def test_codex_runner_invokes_cli_and_records_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake_codex = root / "codex"
            fake_codex.write_text(
                "\n".join(
                    [
                        "#!/bin/sh",
                        "out=''",
                        "while [ \"$#\" -gt 0 ]; do",
                        "  case \"$1\" in",
                        "    --cd) shift; cd \"$1\" ;;",
                        "    --output-last-message) shift; out=\"$1\" ;;",
                        "  esac",
                        "  shift",
                        "done",
                        "pwd > codex-working-dir.txt",
                        "printf 'Fake Codex completed\\n' > \"$out\"",
                        "printf '{\"type\":\"result\"}\\n'",
                    ]
                ),
                encoding="utf-8",
            )
            os.chmod(fake_codex, 0o755)

            context = ExecutionContext(task_id="task-1", path=root / "ctx")
            task = Task(
                id="task-1",
                title="Coding test",
                description="Create a handoff note.",
                source="test",
                state="Todo",
                task_type="coding",
                risk_level=RiskLevel.LOW,
                labels=("agent-ready", "type:coding"),
            )
            runner = CodexRunner(codex_bin=str(fake_codex), timeout_seconds=5)

            result = runner.start_run(task, context, {"name": "test-workflow"})

            self.assertTrue(result.succeeded)
            self.assertTrue(result.completed)
            self.assertEqual(result.summary, "Fake Codex completed")
            self.assertEqual(result.metadata["provider"], "codex")
            self.assertTrue((context.path / "codex-prompt.md").exists())
            self.assertTrue((context.path / "codex-last-message.md").exists())
            self.assertEqual(
                (context.path / "codex-working-dir.txt").read_text(encoding="utf-8").strip(),
                str(context.path.resolve()),
            )

    def test_codex_runner_preserves_approval_gate_for_risky_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake_codex = root / "codex"
            fake_codex.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
            os.chmod(fake_codex, 0o755)

            context = ExecutionContext(task_id="task-2", path=root / "ctx")
            task = Task(
                id="task-2",
                title="Email test",
                description="Draft and send an external email.",
                source="test",
                state="Todo",
                task_type="email",
                risk_level=RiskLevel.MEDIUM,
                labels=("agent-ready", "type:email"),
                target_systems=("email",),
            )
            runner = CodexRunner(codex_bin=str(fake_codex), timeout_seconds=5)

            result = runner.start_run(task, context, {"name": "test-workflow"})

            self.assertTrue(result.succeeded)
            self.assertFalse(result.completed)
            self.assertEqual(len(result.proposals), 1)
            self.assertFalse(result.metadata["codex_invoked"])
            self.assertFalse((context.path / "codex-last-message.md").exists())


if __name__ == "__main__":
    unittest.main()
