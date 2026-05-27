from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from symphony_general.actions import ActionExecutor
from symphony_general.approval import ApprovalGate
from symphony_general.audit import AuditLog
from symphony_general.context import ExecutionContextManager
from symphony_general.daemon import DaemonConfig, SymphonyDaemon
from symphony_general.fixtures import FixturePlaneTaskSource
from symphony_general.orchestrator import Orchestrator
from symphony_general.plane import PlaneClient, PlaneConfig
from symphony_general.runner import CodexRunner, ProposalOnlyRunner


def main() -> int:
    parser = argparse.ArgumentParser(description="General-purpose Symphony orchestrator")
    subparsers = parser.add_subparsers(dest="command", required=True)

    dry = subparsers.add_parser("dry-run", help="Run orchestration against a local fixture")
    dry.add_argument("--fixture", required=True)
    dry.add_argument("--workspace-root", default=".symphony/workspaces")
    dry.add_argument("--audit-log", default=".symphony/audit/events.jsonl")

    smoke = subparsers.add_parser("plane-smoke", help="Check self-hosted Plane project wiring")
    smoke.add_argument("--project-name", default="test project")

    sync_states = subparsers.add_parser("sync-plane-states", help="Ensure a Plane project has Symphony workflow states")
    sync_states.add_argument("--project-name", default="test project")
    sync_states.add_argument("--dry-run", action="store_true")

    poll = subparsers.add_parser("poll-once", help="Run one orchestration polling pass")
    poll.add_argument("--project-name", help="Limit polling to one project; omit for workspace-wide polling")
    poll.add_argument("--dry-run", action="store_true")
    poll.add_argument("--workspace-root", default=".symphony/workspaces")
    poll.add_argument("--audit-log", default=".symphony/audit/events.jsonl")
    poll.add_argument(
        "--max-concurrency",
        type=int,
        default=int(os.environ.get("SYMPHONY_MAX_CONCURRENCY", "1")),
        help="Maximum number of agent runs to execute concurrently",
    )

    daemon = subparsers.add_parser("run-daemon", help="Continuously poll Plane and run eligible work")
    daemon.add_argument("--project-name", help="Limit polling to one project; omit for workspace-wide polling")
    daemon.add_argument("--fixture")
    daemon.add_argument("--dry-run", action="store_true")
    daemon.add_argument("--workspace-root", default=".symphony/workspaces")
    daemon.add_argument("--audit-log", default=".symphony/audit/events.jsonl")
    daemon.add_argument(
        "--max-concurrency",
        type=int,
        default=int(os.environ.get("SYMPHONY_MAX_CONCURRENCY", "1")),
        help="Maximum number of agent runs to execute concurrently",
    )
    daemon.add_argument(
        "--interval-seconds",
        type=float,
        default=float(os.environ.get("SYMPHONY_DAEMON_INTERVAL_SECONDS", "30")),
    )
    daemon.add_argument(
        "--failure-backoff-seconds",
        type=float,
        default=float(os.environ.get("SYMPHONY_DAEMON_FAILURE_BACKOFF_SECONDS", "60")),
    )
    daemon.add_argument("--max-cycles", type=int)

    args = parser.parse_args()

    if args.command == "dry-run":
        source = FixturePlaneTaskSource(Path(args.fixture))
        summary = _build_orchestrator(source, args.workspace_root, args.audit_log, dry_run=True).poll_once()
        print(json.dumps(_summary(summary, source.events), indent=2))
        return 0

    if args.command == "plane-smoke":
        client = PlaneClient(_plane_config_or_exit(parser, args.project_name), dry_run=True)
        print(json.dumps(client.smoke_check(args.project_name), indent=2))
        return 0

    if args.command == "sync-plane-states":
        client = PlaneClient(_plane_config_or_exit(parser, args.project_name), dry_run=args.dry_run)
        print(json.dumps(client.sync_workflow_states(args.project_name), indent=2))
        return 0

    if args.command == "poll-once":
        source = PlaneClient(_plane_config_or_exit(parser, args.project_name), dry_run=args.dry_run)
        summary = _build_orchestrator(
            source,
            args.workspace_root,
            args.audit_log,
            dry_run=args.dry_run,
            max_concurrency=args.max_concurrency,
        ).poll_once()
        print(json.dumps(_summary(summary, []), indent=2))
        return 0

    if args.command == "run-daemon":
        audit_log = AuditLog(Path(args.audit_log))
        daemon_runner = SymphonyDaemon(
            orchestrator_factory=lambda: _build_orchestrator(
                _source_for_daemon(parser, args),
                args.workspace_root,
                args.audit_log,
                dry_run=args.dry_run or bool(args.fixture),
                audit_log=audit_log,
                max_concurrency=args.max_concurrency,
            ),
            audit_log=audit_log,
            config=DaemonConfig(
                interval_seconds=args.interval_seconds,
                failure_backoff_seconds=args.failure_backoff_seconds,
                max_cycles=args.max_cycles,
            ),
        )
        daemon_runner.install_signal_handlers()
        return daemon_runner.run()

    return 1


def _plane_config_or_exit(parser: argparse.ArgumentParser, project_name: str | None) -> PlaneConfig:
    try:
        return PlaneConfig.from_env(project_name)
    except RuntimeError as exc:
        parser.error(str(exc))


def _source_for_daemon(parser: argparse.ArgumentParser, args):
    if args.fixture:
        return FixturePlaneTaskSource(Path(args.fixture))
    return PlaneClient(_plane_config_or_exit(parser, args.project_name), dry_run=args.dry_run)


def _build_orchestrator(
    source,
    workspace_root: str,
    audit_log_path: str,
    dry_run: bool,
    audit_log: AuditLog | None = None,
    max_concurrency: int = 1,
) -> Orchestrator:
    audit_log = audit_log or AuditLog(Path(audit_log_path))
    approval_gate = ApprovalGate(audit_log)
    return Orchestrator(
        task_source=source,
        context_manager=ExecutionContextManager(Path(workspace_root), audit_log),
        runner=_build_runner(),
        approval_gate=approval_gate,
        action_executor=ActionExecutor(approval_gate, audit_log, dry_run=dry_run),
        audit_log=audit_log,
        max_concurrency=max_concurrency,
        workflow={
            "name": "general-purpose-plane-symphony",
            "human_approved_state": "Human Approved",
        },
    )


def _build_runner():
    runner = os.environ.get("SYMPHONY_RUNNER", "proposal-only").strip().lower()
    if runner in {"proposal-only", "proposal", "stub"}:
        return ProposalOnlyRunner()
    if runner == "codex":
        return CodexRunner.from_env()
    raise RuntimeError(f"Unsupported SYMPHONY_RUNNER={runner!r}; expected 'proposal-only' or 'codex'")


def _summary(summary, source_events):
    return {
        "candidates": summary.candidates,
        "claimed": summary.claimed,
        "completed": summary.completed,
        "pending_approval": summary.pending_approval,
        "failed": summary.failed,
        "runs": [
            {
                "run_id": result.run_id,
                "task_id": result.task_id,
                "summary": result.summary,
                "proposal_ids": [proposal.id for proposal in result.proposals],
            }
            for result in summary.results
        ],
        "source_events": source_events,
    }


if __name__ == "__main__":
    raise SystemExit(main())
