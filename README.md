# Symphony General Orchestrator

This repository contains a compact reference design for a general-purpose Symphony-style
agent orchestrator with:

- Plane as the task tracker
- one isolated execution context per task
- a generic agent runner interface
- human approval before irreversible external actions
- structured audit logging

The implementation is intentionally dependency-light and runnable with Python's standard
library so it can be tested before wiring in a real agent runtime. The optional Codex runner
uses the local `codex` CLI for coding tasks.

## Quick Start

```bash
python3 -m unittest discover -s tests
python3 -m symphony_general.cli dry-run --fixture examples/plane_test_project_fixture.json
```

For a self-hosted Plane smoke test against the project named `test project`:

```bash
export PLANE_BASE_URL="https://plane.example.internal"
export PLANE_API_KEY="..."
export PLANE_WORKSPACE_SLUG="your-workspace"

python3 -m symphony_general.cli plane-smoke --project-name "test project"
python3 -m symphony_general.cli sync-plane-states --project-name "test project" --dry-run
python3 -m symphony_general.cli poll-once --project-name "test project" --dry-run
```

`poll-once --dry-run` reads eligible Plane work items and exercises the orchestration path
without mutating Plane state. Remove `--dry-run` only when the test project is safe to update.

To run the worker continuously in the foreground:

```bash
python3 -m symphony_general.cli run-daemon \
  --project-name "test project" \
  --interval-seconds 30
```

For a bounded local daemon check:

```bash
python3 -m symphony_general.cli run-daemon \
  --fixture examples/plane_test_project_fixture.json \
  --interval-seconds 0 \
  --max-cycles 1
```

To run eligible tasks with Codex instead of the deterministic proposal-only runner:

```bash
export SYMPHONY_RUNNER="codex"
export SYMPHONY_CODEX_WORKDIR="/Users/khc/Desktop/symphony-impl"
export SYMPHONY_CODEX_APPROVAL_POLICY="never"

python3 -m symphony_general.cli poll-once --project-name "test project"
```

Optional Codex settings:

- `SYMPHONY_CODEX_BIN`, default `codex`
- `SYMPHONY_CODEX_MODEL`, optional
- `SYMPHONY_CODEX_SANDBOX`, default `workspace-write`
- `SYMPHONY_CODEX_TIMEOUT_SECONDS`, default `900`
- `SYMPHONY_CODEX_EXTRA_ARGS`, shell-style extra arguments passed to `codex exec`
- `SYMPHONY_DAEMON_INTERVAL_SECONDS`, default `30`
- `SYMPHONY_DAEMON_FAILURE_BACKOFF_SECONDS`, default `60`

## Plane Task Contract

The Plane adapter looks for work items in the configured project that are:

- assigned to the configured agent assignee, if one is configured
- in the configured todo state, default `Todo`, or the human approval state, default `Human Approved`
- labeled with the configured ready label, default `agent-ready`

The orchestrator claims work by moving the work item to `In Progress` and adding a comment.
If the agent produces an external action proposal, the item moves to `Needs Human` and waits
for approval. Moving the item to `Human Approved` lets Symphony resume, execute approved
proposals, and close it as `Done`. Completed agent handoff moves to `In Review`; failures move
to `Blocked`.

See [docs/architecture.md](docs/architecture.md) and [WORKFLOW.md](WORKFLOW.md).
