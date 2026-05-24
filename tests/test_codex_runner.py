from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from symphony_general.models import ExecutionContext, RiskLevel, Task
from symphony_general.runner import CodexRunner


class CodexRunnerTest(unittest.TestCase):
    def _fake_codex(self, root: Path) -> Path:
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
        return fake_codex

    def test_codex_runner_invokes_cli_and_records_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake_codex = self._fake_codex(root)

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
                raw={
                    "symphony_recent_comments": [
                        {
                            "id": "comment-0",
                            "created_at": "2026-05-23T11:50:00Z",
                            "text": "Symphony claimed this work item for agent execution.",
                        },
                        {
                            "id": "comment-history",
                            "created_at": "2026-05-23T11:55:00Z",
                            "text": "Symphony completed run run-previous.\n\nBuilt the initial handoff outline.",
                        },
                    ],
                    "symphony_trigger_comment": {
                        "id": "comment-1",
                        "created_at": "2026-05-23T12:00:00Z",
                        "text": "@agent-worker include the interview checklist.",
                    }
                },
            )
            runner = CodexRunner(codex_bin=str(fake_codex), timeout_seconds=5)

            result = runner.start_run(task, context, {"name": "test-workflow"})

            self.assertTrue(result.succeeded)
            self.assertTrue(result.completed)
            self.assertEqual(result.summary, "Fake Codex completed")
            self.assertEqual(result.metadata["provider"], "codex")
            self.assertTrue((context.path / "codex-prompt.md").exists())
            self.assertTrue((context.path / "codex-last-message.md").exists())
            prompt = (context.path / "codex-prompt.md").read_text(encoding="utf-8")
            self.assertIn("## Ticket Context", prompt)
            self.assertIn("Create a handoff note.", prompt)
            self.assertIn("## Prior Symphony History", prompt)
            self.assertIn("Built the initial handoff outline", prompt)
            self.assertNotIn("claimed this work item for agent execution", prompt)
            self.assertIn("## Agent Trigger Comment", prompt)
            self.assertIn("include the interview checklist", prompt)
            self.assertEqual(
                (context.path / "codex-working-dir.txt").read_text(encoding="utf-8").strip(),
                str(context.path.resolve()),
            )

    def test_codex_runner_uses_description_target_path_as_working_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake_codex = self._fake_codex(root)
            target_dir = root / "target-project"
            target_dir.mkdir()
            context = ExecutionContext(task_id="task-target", path=root / "ctx")
            task = Task(
                id="task-target",
                title="Target path task",
                description=f"Create a note.\n\ntarget_path: {target_dir}",
                source="test",
                state="Todo",
                task_type="coding",
                risk_level=RiskLevel.LOW,
                labels=("agent-ready",),
            )
            runner = CodexRunner(codex_bin=str(fake_codex), timeout_seconds=5)

            result = runner.start_run(task, context, {"name": "test-workflow"})

            self.assertTrue(result.succeeded)
            self.assertEqual(
                (target_dir / "codex-working-dir.txt").read_text(encoding="utf-8").strip(),
                str(target_dir.resolve()),
            )
            self.assertEqual(result.metadata["working_dir"], str(target_dir.resolve()))
            self.assertEqual(result.metadata["target_context"]["target_path"], str(target_dir))

    def test_codex_runner_uses_trigger_comment_target_path_before_description(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake_codex = self._fake_codex(root)
            description_target = root / "description-target"
            comment_target = root / "comment-target"
            fallback = root / "fallback"
            description_target.mkdir()
            comment_target.mkdir()
            fallback.mkdir()
            context = ExecutionContext(task_id="task-comment-target", path=root / "ctx")
            task = Task(
                id="task-comment-target",
                title="Comment target path task",
                description=f"Create a note.\n\ntarget_path: {description_target}",
                source="test",
                state="Todo",
                task_type="coding",
                risk_level=RiskLevel.LOW,
                labels=("agent-ready",),
                raw={
                    "symphony_trigger_comment": {
                        "id": "comment-1",
                        "created_at": "2026-05-23T12:00:00Z",
                        "text": f"@agent-worker update this workspace.\n\ntarget_path: {comment_target}",
                    }
                },
            )
            runner = CodexRunner(codex_bin=str(fake_codex), timeout_seconds=5, workdir=fallback)

            result = runner.start_run(task, context, {"name": "test-workflow"})

            self.assertTrue(result.succeeded)
            self.assertEqual(
                (comment_target / "codex-working-dir.txt").read_text(encoding="utf-8").strip(),
                str(comment_target.resolve()),
            )
            self.assertFalse((description_target / "codex-working-dir.txt").exists())
            self.assertFalse((fallback / "codex-working-dir.txt").exists())
            self.assertEqual(result.metadata["target_context"]["source"], "trigger_comment")

    def test_codex_runner_uses_file_target_parent_as_working_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake_codex = self._fake_codex(root)
            target_file = root / "notes" / "handoff.md"
            target_file.parent.mkdir()
            context = ExecutionContext(task_id="task-file-target", path=root / "ctx")
            task = Task(
                id="task-file-target",
                title="File target path task",
                description=f"Create the handoff.\n\ntarget_path: {target_file}",
                source="test",
                state="Todo",
                task_type="coding",
                risk_level=RiskLevel.LOW,
                labels=("agent-ready",),
            )
            runner = CodexRunner(codex_bin=str(fake_codex), timeout_seconds=5)

            result = runner.start_run(task, context, {"name": "test-workflow"})

            self.assertTrue(result.succeeded)
            self.assertEqual(
                (target_file.parent / "codex-working-dir.txt").read_text(encoding="utf-8").strip(),
                str(target_file.parent.resolve()),
            )
            prompt = (context.path / "codex-prompt.md").read_text(encoding="utf-8")
            self.assertIn(f"Requested target_path: {target_file}", prompt)
            self.assertIn("Treat target_path as the requested file path", prompt)
            self.assertEqual(result.metadata["target_context"]["kind"], "file")

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
