from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from uuid import uuid4

from symphony_general.models import ActionProposal, ExecutionContext, RiskLevel, RunResult, Task


class AgentRunner:
    def start_run(self, task: Task, context: ExecutionContext, workflow: dict[str, object]) -> RunResult:
        raise NotImplementedError

    def cancel_run(self, run_id: str) -> None:
        raise NotImplementedError


class ProposalOnlyRunner(AgentRunner):
    """Deterministic runner used until a real agent runtime is connected."""

    def start_run(self, task: Task, context: ExecutionContext, workflow: dict[str, object]) -> RunResult:
        run_id = f"run-{uuid4().hex[:12]}"
        context_file = context.path / "task.md"
        context_file.write_text(
            f"# {task.title}\n\n{task.description}\n\nSource: {task.source}:{task.id}\n",
            encoding="utf-8",
        )

        proposals: tuple[ActionProposal, ...] = ()
        if task.requires_approval or task.risk_level != RiskLevel.LOW or task.target_systems:
            proposals = (
                ActionProposal(
                    task_id=task.id,
                    summary=f"Proposed next action for {task.title}",
                    action_type=task.task_type,
                    payload={
                        "draft": task.description,
                        "context_path": str(context.path),
                    },
                    risk_level=task.risk_level,
                    target_system=task.target_systems[0] if task.target_systems else None,
                    requires_approval=True,
                ),
            )

        return RunResult(
            task_id=task.id,
            run_id=run_id,
            summary=f"Prepared execution context for {task.title}",
            proposals=proposals,
            completed=not proposals,
        )

    def cancel_run(self, run_id: str) -> None:
        return None


class CodexRunner(AgentRunner):
    """Agent runner backed by the local Codex CLI."""

    def __init__(
        self,
        *,
        codex_bin: str = "codex",
        model: str | None = None,
        sandbox: str = "workspace-write",
        approval_policy: str = "never",
        timeout_seconds: int = 900,
        workdir: Path | None = None,
        extra_args: tuple[str, ...] = (),
    ) -> None:
        self.codex_bin = codex_bin
        self.model = model
        self.sandbox = sandbox
        self.approval_policy = approval_policy
        self.timeout_seconds = timeout_seconds
        self.workdir = workdir
        self.extra_args = extra_args

    @classmethod
    def from_env(cls) -> "CodexRunner":
        workdir = os.environ.get("SYMPHONY_CODEX_WORKDIR")
        timeout = os.environ.get("SYMPHONY_CODEX_TIMEOUT_SECONDS", "900")
        return cls(
            codex_bin=os.environ.get("SYMPHONY_CODEX_BIN", "codex"),
            model=os.environ.get("SYMPHONY_CODEX_MODEL") or None,
            sandbox=os.environ.get("SYMPHONY_CODEX_SANDBOX", "workspace-write"),
            approval_policy=os.environ.get("SYMPHONY_CODEX_APPROVAL_POLICY", "never"),
            timeout_seconds=int(timeout),
            workdir=Path(workdir).expanduser() if workdir else None,
            extra_args=tuple(shlex.split(os.environ.get("SYMPHONY_CODEX_EXTRA_ARGS", ""))),
        )

    def start_run(self, task: Task, context: ExecutionContext, workflow: dict[str, object]) -> RunResult:
        run_id = f"run-{uuid4().hex[:12]}"
        context.path.mkdir(parents=True, exist_ok=True)

        task_file = context.path / "task.md"
        task_file.write_text(self._task_markdown(task), encoding="utf-8")
        proposals = self._approval_proposals(task, context)
        if proposals:
            return RunResult(
                task_id=task.id,
                run_id=run_id,
                summary=f"Prepared approval proposal for {task.title}",
                proposals=proposals,
                completed=False,
                metadata={
                    "provider": "codex",
                    "runner": "CodexRunner",
                    "codex_invoked": False,
                    "reason": "approval_required",
                },
            )

        prompt_file = context.path / "codex-prompt.md"
        stdout_file = context.path / "codex-stdout.jsonl"
        stderr_file = context.path / "codex-stderr.log"
        last_message_file = context.path / "codex-last-message.md"
        working_dir = (self.workdir or context.path).resolve()
        prompt = self._build_prompt(task, context, workflow, working_dir)

        prompt_file.write_text(prompt, encoding="utf-8")

        command = self._command(working_dir, last_message_file, prompt_file)
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except FileNotFoundError:
            return RunResult(
                task_id=task.id,
                run_id=run_id,
                summary="Codex CLI was not found.",
                completed=False,
                error=f"Codex binary not found: {self.codex_bin}",
                metadata=self._metadata(command, working_dir, prompt_file, last_message_file),
            )
        except subprocess.TimeoutExpired as exc:
            stdout_file.write_text(exc.stdout or "", encoding="utf-8")
            stderr_file.write_text(exc.stderr or "", encoding="utf-8")
            return RunResult(
                task_id=task.id,
                run_id=run_id,
                summary=f"Codex timed out after {self.timeout_seconds} seconds.",
                completed=False,
                error="codex timeout",
                metadata=self._metadata(command, working_dir, prompt_file, last_message_file),
            )

        stdout_file.write_text(completed.stdout, encoding="utf-8")
        stderr_file.write_text(completed.stderr, encoding="utf-8")
        summary = self._read_summary(last_message_file, completed.stdout)
        metadata = self._metadata(command, working_dir, prompt_file, last_message_file)
        metadata["exit_code"] = completed.returncode
        metadata["stdout_path"] = str(stdout_file)
        metadata["stderr_path"] = str(stderr_file)

        if completed.returncode != 0:
            error = self._failure_text(completed.returncode, completed.stderr, completed.stdout)
            return RunResult(
                task_id=task.id,
                run_id=run_id,
                summary=summary or "Codex run failed.",
                completed=False,
                error=error,
                metadata=metadata,
            )

        return RunResult(
            task_id=task.id,
            run_id=run_id,
            summary=summary or f"Codex completed {task.title}",
            completed=True,
            metadata=metadata,
        )

    def cancel_run(self, run_id: str) -> None:
        return None

    def _command(self, working_dir: Path, last_message_file: Path, prompt_file: Path) -> list[str]:
        command = [
            self.codex_bin,
            "exec",
            "--cd",
            str(working_dir),
            "--sandbox",
            self.sandbox,
            "--ask-for-approval",
            self.approval_policy,
            "--skip-git-repo-check",
            "--json",
            "--output-last-message",
            str(last_message_file),
        ]
        if self.model:
            command.extend(["--model", self.model])
        command.extend(self.extra_args)
        command.append(f"Run the Symphony task described in {prompt_file}.")
        return command

    def _metadata(
        self,
        command: list[str],
        working_dir: Path,
        prompt_file: Path,
        last_message_file: Path,
    ) -> dict[str, object]:
        return {
            "provider": "codex",
            "runner": "CodexRunner",
            "command": command,
            "working_dir": str(working_dir),
            "prompt_path": str(prompt_file),
            "last_message_path": str(last_message_file),
        }

    def _approval_proposals(self, task: Task, context: ExecutionContext) -> tuple[ActionProposal, ...]:
        if not (task.requires_approval or task.risk_level != RiskLevel.LOW or task.target_systems):
            return ()
        return (
            ActionProposal(
                task_id=task.id,
                summary=f"Proposed next action for {task.title}",
                action_type=task.task_type,
                payload={
                    "draft": task.description,
                    "context_path": str(context.path),
                },
                risk_level=task.risk_level,
                target_system=task.target_systems[0] if task.target_systems else None,
                requires_approval=True,
            ),
        )

    def _build_prompt(
        self,
        task: Task,
        context: ExecutionContext,
        workflow: dict[str, object],
        working_dir: Path,
    ) -> str:
        labels = ", ".join(task.labels) if task.labels else "none"
        capabilities = ", ".join(task.required_capabilities) if task.required_capabilities else "none"
        target_systems = ", ".join(task.target_systems) if task.target_systems else "none"
        return "\n".join(
            [
                "# Symphony Codex Task",
                "",
                "You are running as a non-interactive coding agent for Symphony.",
                "Work only inside the configured working directory unless the task explicitly requires otherwise.",
                "Keep changes scoped to the ticket, run relevant checks when feasible, and finish with a concise summary plus verification.",
                "Do not perform irreversible external side effects such as sending email, publishing content, or mutating production SaaS records.",
                "",
                f"Workflow: {workflow.get('name', 'symphony')}",
                f"Task ID: {task.id}",
                f"Title: {task.title}",
                f"Source: {task.source}",
                f"State: {task.state}",
                f"Type: {task.task_type}",
                f"Risk: {task.risk_level}",
                f"Labels: {labels}",
                f"Required capabilities: {capabilities}",
                f"Target systems: {target_systems}",
                f"Execution context: {context.path.resolve()}",
                f"Working directory: {working_dir}",
                "",
                "## Description",
                task.description or "(no description provided)",
                "",
                "## Expected Output",
                "Complete the requested coding work when possible. If the task cannot be completed, explain the blocker clearly.",
            ]
        )

    def _task_markdown(self, task: Task) -> str:
        labels = ", ".join(task.labels) if task.labels else "none"
        return "\n".join(
            [
                f"# {task.title}",
                "",
                task.description or "(no description provided)",
                "",
                f"Source: {task.source}:{task.id}",
                f"State: {task.state}",
                f"Type: {task.task_type}",
                f"Risk: {task.risk_level}",
                f"Labels: {labels}",
                "",
            ]
        )

    def _read_summary(self, last_message_file: Path, stdout: str) -> str:
        if last_message_file.exists():
            content = last_message_file.read_text(encoding="utf-8").strip()
            if content:
                return content
        return "\n".join(line for line in stdout.strip().splitlines()[-5:]).strip()

    def _failure_text(self, returncode: int, stderr: str, stdout: str) -> str:
        detail = stderr.strip() or stdout.strip()
        if len(detail) > 1000:
            detail = f"{detail[:1000]}..."
        return f"codex exited with {returncode}: {detail}"
