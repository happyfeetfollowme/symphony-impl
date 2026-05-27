from __future__ import annotations

import json
import signal
import sys
from collections.abc import Callable
from dataclasses import dataclass
from threading import Event
from typing import TextIO

from symphony_general.audit import AuditLog
from symphony_general.models import utc_now
from symphony_general.orchestrator import Orchestrator, PollSummary


@dataclass(frozen=True)
class DaemonConfig:
    interval_seconds: float = 30.0
    failure_backoff_seconds: float = 60.0
    max_cycles: int | None = None


class SymphonyDaemon:
    def __init__(
        self,
        orchestrator_factory: Callable[[], Orchestrator],
        audit_log: AuditLog,
        config: DaemonConfig | None = None,
        output: TextIO | None = None,
    ) -> None:
        self.orchestrator_factory = orchestrator_factory
        self.audit_log = audit_log
        self.config = config or DaemonConfig()
        self.output = output or sys.stdout
        self._stop_requested = Event()

    def install_signal_handlers(self) -> None:
        def request_stop(signum, _frame) -> None:
            self.request_stop(f"signal:{signum}")

        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGTERM, request_stop)

    def request_stop(self, reason: str = "requested") -> None:
        self.audit_log.record("daemon.stop_requested", {"reason": reason})
        self._stop_requested.set()

    def run(self) -> int:
        self.audit_log.record("daemon.started", {"config": self.config})
        self._emit(
            {
                "event": "daemon.started",
                "created_at": utc_now(),
                "interval_seconds": self.config.interval_seconds,
                "failure_backoff_seconds": self.config.failure_backoff_seconds,
                "max_cycles": self.config.max_cycles,
            }
        )

        cycle = 0
        orchestrator = self.orchestrator_factory()
        try:
            while not self._stop_requested.is_set():
                cycle += 1
                sleep_seconds = self.config.interval_seconds

                try:
                    summary = orchestrator.poll_once(wait_for_completion=False)
                    payload = {
                        "event": "daemon.cycle",
                        "created_at": utc_now(),
                        "cycle": cycle,
                        "summary": poll_summary_dict(summary),
                    }
                    self.audit_log.record("daemon.cycle", payload)
                    self._emit(payload)
                except Exception as exc:  # noqa: BLE001 - daemon boundary converts errors to logs.
                    sleep_seconds = self.config.failure_backoff_seconds
                    payload = {
                        "event": "daemon.cycle_failed",
                        "created_at": utc_now(),
                        "cycle": cycle,
                        "error": str(exc),
                    }
                    self.audit_log.record("daemon.cycle_failed", payload)
                    self._emit(payload)

                if self.config.max_cycles is not None and cycle >= self.config.max_cycles:
                    break

                if sleep_seconds > 0:
                    self._stop_requested.wait(sleep_seconds)
        finally:
            if hasattr(orchestrator, "drain_running"):
                summary = orchestrator.drain_running()
                if summary.results or summary.completed or summary.pending_approval or summary.failed:
                    payload = {
                        "event": "daemon.drain",
                        "created_at": utc_now(),
                        "cycle": cycle,
                        "summary": poll_summary_dict(summary),
                    }
                    self.audit_log.record("daemon.drain", payload)
                    self._emit(payload)
            if hasattr(orchestrator, "shutdown"):
                orchestrator.shutdown(wait_for_running=True)

        stop_payload = {
            "event": "daemon.stopped",
            "created_at": utc_now(),
            "cycles": cycle,
            "stop_requested": self._stop_requested.is_set(),
        }
        self.audit_log.record("daemon.stopped", stop_payload)
        self._emit(stop_payload)
        return 0

    def _emit(self, payload: dict[str, object]) -> None:
        self.output.write(json.dumps(payload, sort_keys=True) + "\n")
        self.output.flush()


def poll_summary_dict(summary: PollSummary) -> dict[str, object]:
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
                "completed": result.completed,
                "error": result.error,
                "metadata": result.metadata,
            }
            for result in summary.results
        ],
    }
