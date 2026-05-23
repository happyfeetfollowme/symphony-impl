# Symphony Workflow

This workflow describes the local orchestration policy. It is read by humans and can also be
loaded by automation as a simple key-value contract.

```yaml
name: general-purpose-plane-symphony
tracker:
  kind: plane
  scope: workspace
  optional_project_name: test project
  ready_label: agent-ready
  agent_assignee: agent-worker
  states:
    todo: Todo
    in_progress: In Progress
    needs_human: Needs Human
    human_approved: Human Approved
    in_review: In Review
    blocked: Blocked
    done: Done

execution_context:
  root: .symphony/workspaces
  isolation: one-directory-per-task
  retain_after_completion: true

daemon:
  command: python3 -m symphony_general.cli run-daemon
  interval_seconds: 30
  failure_backoff_seconds: 60
  stop_signals:
    - SIGINT
    - SIGTERM
  log_format: jsonl_stdout

runner:
  kind: proposal-only | codex
  contract:
    start_run: task, execution_context, workflow
    cancel_run: run_id
  codex:
    env_switch: SYMPHONY_RUNNER=codex
    command: codex exec
    default_sandbox: workspace-write
    default_approval_policy: never
    optional_workdir_env: SYMPHONY_CODEX_WORKDIR

approval:
  default_required: true
  require_for_risk_levels:
    - medium
    - high
  require_for_external_actions: true
  approval_state: Needs Human
  approved_state: Human Approved

actions:
  default_mode: proposal
  irreversible_actions_require_approval: true

audit:
  path: .symphony/audit/events.jsonl
```

## Human Approval Rules

Agents may prepare proposals, drafts, branch names, pull request summaries, or structured
payloads. They must not directly execute irreversible external actions such as sending email,
publishing content, submitting forms, or mutating production SaaS records unless an approval
decision has been recorded.

Approval decisions should include:

- `approved`, `rejected`, or `changes_requested`
- approver identity
- timestamp
- optional rationale
- the proposal id being approved

## Plane Mapping

| Plane | Symphony |
| --- | --- |
| work item id | task id |
| work item title | task title |
| description | task context |
| state | visible lifecycle |
| labels | routing and policy |
| assignees | agent selection |
| comments | run log and handoff |
| project | workflow mapping |

The orchestrator keeps its internal state separate from Plane state. Plane remains the human
coordination surface; Symphony owns run ids, retries, execution contexts, and audit events.
