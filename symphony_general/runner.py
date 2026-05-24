from __future__ import annotations

from dataclasses import dataclass
import os
import re
import shlex
import subprocess
from pathlib import Path
from uuid import uuid4

from symphony_general.models import ActionProposal, ExecutionContext, RiskLevel, RunResult, Task


@dataclass(frozen=True)
class _TargetPath:
    path: Path
    source: str


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

    TARGET_PATH_PATTERN = re.compile(r"^\s*target_path\s*:\s*(?P<path>.+?)\s*$", re.IGNORECASE | re.MULTILINE)

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
        context_path = context.path.resolve()

        task_file = context_path / "task.md"
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

        prompt_file = context_path / "codex-prompt.md"
        stdout_file = context_path / "codex-stdout.jsonl"
        stderr_file = context_path / "codex-stderr.log"
        last_message_file = context_path / "codex-last-message.md"
        working_dir, target_context = self._resolve_working_dir(task, context_path)
        prompt = self._build_prompt(task, context, workflow, working_dir, target_context)

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
                metadata=self._metadata(command, working_dir, prompt_file, last_message_file, target_context),
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
                metadata=self._metadata(command, working_dir, prompt_file, last_message_file, target_context),
            )

        stdout_file.write_text(completed.stdout, encoding="utf-8")
        stderr_file.write_text(completed.stderr, encoding="utf-8")
        summary = self._read_summary(last_message_file, completed.stdout)
        metadata = self._metadata(command, working_dir, prompt_file, last_message_file, target_context)
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
        target_context: dict[str, object] | None = None,
    ) -> dict[str, object]:
        metadata: dict[str, object] = {
            "provider": "codex",
            "runner": "CodexRunner",
            "command": command,
            "working_dir": str(working_dir),
            "prompt_path": str(prompt_file),
            "last_message_path": str(last_message_file),
        }
        if target_context:
            metadata["target_context"] = target_context
        return metadata

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
        target_context: dict[str, object] | None = None,
    ) -> str:
        labels = ", ".join(task.labels) if task.labels else "none"
        capabilities = ", ".join(task.required_capabilities) if task.required_capabilities else "none"
        target_systems = ", ".join(task.target_systems) if task.target_systems else "none"
        lines = [
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
            "## Ticket Context",
            task.description or "(no description provided)",
        ]
        if target_context:
            lines.extend(
                [
                    "",
                    "## Target Path",
                    f"Requested target_path: {target_context['target_path']}",
                    f"Target source: {target_context['source']}",
                    f"Resolved working directory: {target_context['working_dir']}",
                ]
            )
            if target_context["kind"] == "file":
                lines.append("Treat target_path as the requested file path; create or update that file when the task asks for an output artifact.")
            elif target_context["kind"] == "directory":
                lines.append("Treat target_path as the requested project or output directory.")
            if target_context.get("warning"):
                lines.append(f"Warning: {target_context['warning']}")
        history_comments = self._symphony_history_comments(task)
        if history_comments:
            lines.extend(["", "## Prior Symphony History"])
            for comment in history_comments:
                lines.extend(
                    [
                        f"Comment ID: {comment.get('id', '')}",
                        f"Created at: {comment.get('created_at', '')}",
                        "",
                        str(comment.get("text") or ""),
                        "",
                    ]
                )
        trigger_comment = self._trigger_comment(task)
        if trigger_comment:
            lines.extend(
                [
                    "",
                    "## Agent Trigger Comment",
                    f"Comment ID: {trigger_comment.get('id', '')}",
                    f"Created at: {trigger_comment.get('created_at', '')}",
                    "",
                    str(trigger_comment.get("text") or ""),
                ]
            )
        lines.extend(
            [
                "",
                "## Expected Output",
                "Complete the requested coding work when possible. If the task cannot be completed, explain the blocker clearly.",
            ]
        )
        return "\n".join(lines)

    def _task_markdown(self, task: Task) -> str:
        labels = ", ".join(task.labels) if task.labels else "none"
        lines = [
            f"# {task.title}",
            "",
            task.description or "(no description provided)",
            "",
            f"Source: {task.source}:{task.id}",
            f"State: {task.state}",
            f"Type: {task.task_type}",
            f"Risk: {task.risk_level}",
            f"Labels: {labels}",
        ]
        trigger_comment = self._trigger_comment(task)
        if trigger_comment:
            lines.extend(["", "## Agent Trigger Comment", str(trigger_comment.get("text") or "")])
        lines.append("")
        return "\n".join(lines)

    def _trigger_comment(self, task: Task) -> dict[str, object] | None:
        comment = task.raw.get("symphony_trigger_comment")
        return comment if isinstance(comment, dict) else None

    def _recent_comments(self, task: Task) -> tuple[dict[str, object], ...]:
        comments = task.raw.get("symphony_recent_comments")
        if not isinstance(comments, list):
            return ()
        return tuple(comment for comment in comments if isinstance(comment, dict))

    def _symphony_history_comments(self, task: Task, limit: int = 3) -> tuple[dict[str, object], ...]:
        history = [
            comment
            for comment in self._recent_comments(task)
            if self._is_symphony_history_comment(str(comment.get("text") or ""))
        ]
        history.sort(key=lambda comment: str(comment.get("created_at") or ""))
        return tuple(history[-limit:])

    def _is_symphony_history_comment(self, text: str) -> bool:
        normalized = text.strip()
        if not normalized.casefold().startswith("symphony "):
            return False
        return "claimed this work item" not in normalized.casefold()

    def _resolve_working_dir(self, task: Task, context_path: Path) -> tuple[Path, dict[str, object] | None]:
        target = self._target_path(task)
        if not target:
            return (self.workdir or context_path).resolve(), None

        target_working_dir = self._working_dir_for_target(target.path)
        working_dir = target_working_dir or (self.workdir or context_path).resolve()
        target_context: dict[str, object] = {
            "target_path": str(target.path),
            "source": target.source,
            "kind": self._target_kind(target.path),
            "working_dir": str(working_dir),
        }
        if not target_working_dir:
            target_context["warning"] = (
                "target_path could not be used as a working directory because neither it nor its parent exists."
            )
        return working_dir, target_context

    def _working_dir_for_target(self, target_path: Path) -> Path | None:
        if target_path.exists() and target_path.is_dir():
            return target_path.resolve()
        parent = target_path.parent
        if parent.exists() and parent.is_dir():
            return parent.resolve()
        return None

    def _target_kind(self, target_path: Path) -> str:
        if target_path.exists() and target_path.is_dir():
            return "directory"
        if target_path.exists() and target_path.is_file():
            return "file"
        if target_path.suffix:
            return "file"
        return "directory"

    def _target_path(self, task: Task) -> _TargetPath | None:
        trigger_comment = self._trigger_comment(task)
        sources: tuple[tuple[str, str], ...] = (
            ("trigger_comment", str(trigger_comment.get("text") or "") if trigger_comment else ""),
            ("description", task.description or ""),
        )
        for source, text in sources:
            match = self.TARGET_PATH_PATTERN.search(text)
            if not match:
                continue
            value = match.group("path").strip().strip("'\"")
            path = Path(value).expanduser()
            if path.is_absolute():
                return _TargetPath(path=path, source=source)
        return None

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
