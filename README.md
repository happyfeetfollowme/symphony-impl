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
python3 -m symphony_general.cli poll-once --dry-run
```

`poll-once --dry-run` reads eligible Plane work items across the workspace and exercises the
orchestration path without mutating Plane state. Add `--project-name "test project"` only when
you want to limit polling to one project. Remove `--dry-run` only when the workspace is safe to
update.

To run the worker continuously in the foreground:

```bash
python3 -m symphony_general.cli run-daemon \
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
export SYMPHONY_MAX_CONCURRENCY="1"

python3 -m symphony_general.cli poll-once
```

Optional Codex settings:

- `SYMPHONY_CODEX_BIN`, default `codex`
- `SYMPHONY_CODEX_MODEL`, optional
- `SYMPHONY_CODEX_SANDBOX`, default `workspace-write`
- `SYMPHONY_CODEX_TIMEOUT_SECONDS`, default `900`
- `SYMPHONY_CODEX_EXTRA_ARGS`, shell-style extra arguments passed to `codex exec`
- `SYMPHONY_MAX_CONCURRENCY`, default `1`; set above `1` to run multiple eligible tickets concurrently
- `SYMPHONY_DAEMON_INTERVAL_SECONDS`, default `30`
- `SYMPHONY_DAEMON_FAILURE_BACKOFF_SECONDS`, default `60`

`poll-once` waits for the tickets it dispatches and lets the orchestrator write summaries and state
transitions back to Plane. `run-daemon` keeps the orchestrator alive across polling cycles, dispatches
up to the concurrency limit, and reaps completed agent runs on later cycles while retaining the same
orchestrator-owned Plane sync behavior.

## Plane Task Contract

The Plane adapter looks for work items across the workspace, or in one configured project when
`--project-name` is supplied, that are:

- assigned to the configured agent assignee, if one is configured
- in the configured todo state, default `Todo`, or the human approval state, default `Human Approved`
- labeled with the configured ready label, default `agent-ready`

The orchestrator claims work by moving the work item to `In Progress` and adding a comment.
If the agent produces an external action proposal, the item moves to `Needs Human` and waits
for approval. Moving the item to `Human Approved` lets Symphony resume, execute approved
proposals, and close it as `Done`. Completed agent handoff moves to `In Review`; failures move
to `Blocked`.

If a human adds a Plane comment mentioning `agent-worker`, Symphony includes the latest matching
comment as an additional agent prompt when the item is picked up. This does not change pickup
rules by itself; the work item still needs the normal ready label, assignee, and pickup state.

Tickets and `agent-worker` trigger comments can also specify a Codex working location:

```text
target_path: /absolute/path/to/project-or-file
```

When present, `target_path` takes precedence over `SYMPHONY_CODEX_WORKDIR`. A directory path is
used as the Codex working directory. A file path uses its parent directory as the working directory
and is included in the prompt as the requested file to create or update. If no `target_path` is
provided, Symphony keeps the existing fallback order: `SYMPHONY_CODEX_WORKDIR`, then the isolated
task workspace.

See [docs/architecture.md](docs/architecture.md) and [WORKFLOW.md](WORKFLOW.md).
