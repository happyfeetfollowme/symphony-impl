from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html import escape
from html.parser import HTMLParser
from typing import Any, Iterable

from symphony_general.models import RiskLevel, Task


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data.strip())

    def text(self) -> str:
        return "\n".join(self.parts)


@dataclass(frozen=True)
class PlaneConfig:
    base_url: str
    api_key: str
    workspace_slug: str
    project_id: str | None = None
    project_name: str = "test project"
    ready_label: str = "agent-ready"
    agent_assignee: str | None = "agent-worker"
    todo_state: str = "Todo"
    in_progress_state: str = "In Progress"
    needs_human_state: str = "Needs Human"
    human_approved_state: str = "Human Approved"
    in_review_state: str = "In Review"
    blocked_state: str = "Blocked"
    done_state: str = "Done"

    @classmethod
    def from_env(cls, project_name: str = "test project") -> "PlaneConfig":
        missing = [
            key
            for key in ("PLANE_BASE_URL", "PLANE_API_KEY", "PLANE_WORKSPACE_SLUG")
            if not os.environ.get(key)
        ]
        if missing:
            raise RuntimeError(f"Missing Plane environment variables: {', '.join(missing)}")
        return cls(
            base_url=os.environ["PLANE_BASE_URL"],
            api_key=os.environ["PLANE_API_KEY"],
            workspace_slug=os.environ["PLANE_WORKSPACE_SLUG"],
            project_name=project_name,
            ready_label=os.environ.get("SYMPHONY_READY_LABEL", "agent-ready"),
            agent_assignee=os.environ.get("SYMPHONY_AGENT_ASSIGNEE", "agent-worker"),
            human_approved_state=os.environ.get("SYMPHONY_HUMAN_APPROVED_STATE", "Human Approved"),
        )


class PlaneClient:
    def __init__(self, config: PlaneConfig, dry_run: bool = False) -> None:
        self.config = config
        self.dry_run = dry_run
        self.base_api = (
            config.base_url.rstrip("/")
            + "/api/v1/workspaces/"
            + urllib.parse.quote(config.workspace_slug)
        )

    def list_projects(self) -> list[dict[str, Any]]:
        return self._get_paginated(f"{self.base_api}/projects/")

    def find_project(self, name: str | None = None) -> dict[str, Any]:
        expected = (name or self.config.project_name).casefold()
        for project in self.list_projects():
            if str(project.get("name", "")).casefold() == expected:
                return project
        raise LookupError(f"Plane project not found: {name or self.config.project_name}")

    def ensure_project_id(self) -> str:
        if self.config.project_id:
            return self.config.project_id
        return str(self.find_project(self.config.project_name)["id"])

    def list_states(self, project_id: str | None = None) -> list[dict[str, Any]]:
        project_id = project_id or self.ensure_project_id()
        return self._get_paginated(f"{self.base_api}/projects/{project_id}/states/")

    def list_labels(self, project_id: str | None = None) -> list[dict[str, Any]]:
        project_id = project_id or self.ensure_project_id()
        return self._get_paginated(f"{self.base_api}/projects/{project_id}/labels/")

    def list_work_items(self, project_id: str | None = None) -> list[dict[str, Any]]:
        project_id = project_id or self.ensure_project_id()
        query = urllib.parse.urlencode({"expand": "state,labels,assignees"})
        return self._get_paginated(f"{self.base_api}/projects/{project_id}/work-items/?{query}")

    def list_candidate_tasks(self, project_id: str | None = None) -> list[Task]:
        return [
            task
            for item in self.list_work_items(project_id)
            if (task := self._task_from_work_item(item))
            and self._is_candidate(task)
        ]

    def claim_task(self, task: Task) -> None:
        self.update_state(task.id, self.config.in_progress_state)
        self.add_comment(task.id, "Symphony claimed this work item for agent execution.")

    def mark_needs_human(self, task: Task, body: str) -> None:
        self.update_state(task.id, self.config.needs_human_state)
        self.add_comment(task.id, body)

    def sync_success(self, task: Task, body: str) -> None:
        self.update_state(task.id, self.config.in_review_state)
        self.add_comment(task.id, body)

    def sync_done(self, task: Task, body: str) -> None:
        self.update_state(task.id, self.config.done_state)
        self.add_comment(task.id, body)

    def sync_failure(self, task: Task, body: str) -> None:
        self.update_state(task.id, self.config.blocked_state)
        self.add_comment(task.id, body)

    def update_state(self, work_item_id: str, state_name: str) -> None:
        state_id = self._state_id_by_name(state_name)
        self._patch(f"{self._work_item_url(work_item_id)}/", {"state": state_id})

    def add_comment(self, work_item_id: str, body: str) -> None:
        comment_html = "<p>" + escape(body).replace("\n", "<br>") + "</p>"
        self._post(f"{self._work_item_url(work_item_id)}/comments/", {"comment_html": comment_html})

    def smoke_check(self, project_name: str = "test project") -> dict[str, Any]:
        project = self.find_project(project_name)
        project_id = str(project["id"])
        states = [str(state.get("name", "")) for state in self.list_states(project_id)]
        labels = [str(label.get("name", "")) for label in self.list_labels(project_id)]
        items = self.list_work_items(project_id)
        return {
            "project": {"id": project_id, "name": project.get("name")},
            "states": states,
            "labels": labels,
            "work_item_count": len(items),
            "candidate_count": len([item for item in items if self._is_candidate(self._task_from_work_item(item))]),
        }

    def sync_workflow_states(self, project_name: str | None = None) -> dict[str, Any]:
        project = self.find_project(project_name or self.config.project_name)
        project_id = str(project["id"])
        desired = [
            {
                "name": self.config.todo_state,
                "color": "#60646C",
                "group": "unstarted",
                "sequence": 20000.0,
            },
            {
                "name": self.config.in_progress_state,
                "color": "#F59E0B",
                "group": "started",
                "sequence": 30000.0,
            },
            {
                "name": self.config.needs_human_state,
                "color": "#EF4444",
                "group": "started",
                "sequence": 38000.0,
            },
            {
                "name": self.config.human_approved_state,
                "color": "#22C55E",
                "group": "started",
                "sequence": 39000.0,
            },
            {
                "name": self.config.in_review_state,
                "color": "#8B5CF6",
                "group": "started",
                "sequence": 45000.0,
            },
            {
                "name": self.config.blocked_state,
                "color": "#DC2626",
                "group": "cancelled",
                "sequence": 80000.0,
            },
            {
                "name": self.config.done_state,
                "color": "#46A758",
                "group": "completed",
                "sequence": 90000.0,
            },
        ]
        existing = {str(state.get("name", "")).casefold(): state for state in self.list_states(project_id)}
        created: list[str] = []
        skipped: list[str] = []
        responses: list[Any] = []
        url = f"{self.base_api}/projects/{project_id}/states/"

        for state in desired:
            name = str(state["name"])
            if name.casefold() in existing:
                skipped.append(name)
                continue
            payload = {
                **state,
                "description": "Symphony workflow state",
            }
            responses.append(self._post(url, payload))
            created.append(name)

        return {
            "project": {"id": project_id, "name": project.get("name")},
            "created": created,
            "skipped": skipped,
            "dry_run": self.dry_run,
            "responses": responses,
            "states": [str(state.get("name", "")) for state in self.list_states(project_id)] if not self.dry_run else [],
        }

    def _work_item_url(self, work_item_id: str) -> str:
        project_id = self.ensure_project_id()
        return f"{self.base_api}/projects/{project_id}/work-items/{work_item_id}"

    def _state_id_by_name(self, state_name: str) -> str:
        for state in self.list_states():
            if str(state.get("name", "")).casefold() == state_name.casefold():
                return str(state["id"])
        raise LookupError(f"Plane state not found: {state_name}")

    def _is_candidate(self, task: Task | None) -> bool:
        if task is None:
            return False
        if task.state.casefold() not in {
            self.config.todo_state.casefold(),
            self.config.human_approved_state.casefold(),
        }:
            return False
        if self.config.ready_label not in task.labels:
            return False
        if self.config.agent_assignee and self.config.agent_assignee not in task.assignees:
            return False
        return True

    def _task_from_work_item(self, item: dict[str, Any]) -> Task:
        labels = tuple(self._names(item.get("labels", [])))
        assignees = tuple(self._names(item.get("assignees", [])))
        description = self._description(item)
        risk = self._risk_from_labels(labels)
        task_type = self._label_value(labels, "type") or "general"
        target_system = self._target_from_type(task_type)
        return Task(
            id=str(item.get("id") or item.get("issue_id") or item.get("sequence_id")),
            title=str(item.get("name") or item.get("title") or "Untitled Plane work item"),
            description=description,
            source="plane",
            state=str(self._name(item.get("state")) or item.get("state_detail", {}).get("name") or ""),
            labels=labels,
            assignees=assignees,
            project_id=str(item.get("project_id") or item.get("project") or self.config.project_id or ""),
            project_name=self.config.project_name,
            url=item.get("url"),
            task_type=task_type,
            risk_level=risk,
            requires_approval=risk != RiskLevel.LOW or bool(target_system),
            target_systems=(target_system,) if target_system else (),
            raw=item,
        )

    def _description(self, item: dict[str, Any]) -> str:
        if item.get("description_stripped"):
            return str(item["description_stripped"])
        if item.get("description_html"):
            parser = _HTMLTextExtractor()
            parser.feed(str(item["description_html"]))
            return parser.text()
        return str(item.get("description") or "")

    def _risk_from_labels(self, labels: Iterable[str]) -> RiskLevel:
        label = self._label_value(labels, "risk")
        if label in {RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH}:
            return RiskLevel(label)
        return RiskLevel.LOW

    def _label_value(self, labels: Iterable[str], prefix: str) -> str | None:
        marker = f"{prefix}:"
        for label in labels:
            if label.startswith(marker):
                return label[len(marker) :]
        return None

    def _target_from_type(self, task_type: str) -> str | None:
        if task_type in {"email", "social", "form", "saas"}:
            return task_type
        return None

    def _names(self, values: Any) -> list[str]:
        if not isinstance(values, list):
            return []
        return [name for value in values if (name := self._name(value))]

    def _name(self, value: Any) -> str | None:
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            for key in ("name", "display_name", "email", "username"):
                if value.get(key):
                    return str(value[key])
        return None

    def _get_paginated(self, url: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        next_url: str | None = url
        while next_url:
            payload = self._request("GET", next_url)
            if isinstance(payload, list):
                results.extend(payload)
                break
            if not isinstance(payload, dict):
                break
            values = payload.get("results") or payload.get("data") or []
            if isinstance(values, list):
                results.extend(values)
            raw_next = payload.get("next")
            next_url = str(raw_next) if raw_next else None
        return results

    def _get(self, url: str) -> Any:
        return self._request("GET", url)

    def _post(self, url: str, payload: dict[str, Any]) -> Any:
        if self.dry_run:
            return {"dry_run": True, "method": "POST", "url": self._redact(url), "payload": payload}
        return self._request("POST", url, payload)

    def _patch(self, url: str, payload: dict[str, Any]) -> Any:
        if self.dry_run:
            return {"dry_run": True, "method": "PATCH", "url": self._redact(url), "payload": payload}
        return self._request("PATCH", url, payload)

    def _request(self, method: str, url: str, payload: dict[str, Any] | None = None) -> Any:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Content-Type": "application/json",
                "X-Api-Key": self.config.api_key,
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
        return json.loads(raw) if raw else {}

    def _redact(self, url: str) -> str:
        return re.sub(re.escape(self.config.api_key), "***", url)
