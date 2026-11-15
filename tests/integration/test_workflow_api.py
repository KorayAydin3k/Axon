from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from io import BytesIO
from json import dumps
from os import getenv
from secrets import token_urlsafe
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.engine import workflow_engine
from app.persistence.models import FollowupTask, Workflow, WorkflowEvent, WorkflowStep
from app.tools.registry import PermanentToolError, RetryableToolError, SUPPORT_TOOLS


@pytest.fixture
def scenario(monkeypatch):
    if not getenv("DATABASE_URL"):
        pytest.skip("Requires a PostgreSQL DATABASE_URL")

    from app.main import app
    from app.persistence.database import engine

    token = token_urlsafe(32)
    monkeypatch.setenv("AXON_APPROVAL_TOKEN", token)

    with TestClient(app) as client:
        response = client.post("/workflows", json={"request": "Test support follow-up"})
        assert response.status_code == 201, response.text
        workflow_id = UUID(response.json()["id"])
        path = f"/workflows/{workflow_id}"

        try:
            yield client, path, workflow_id, {"X-Axon-Approval-Token": token}
        finally:
            with Session(engine) as session, session.begin():
                for model in (FollowupTask, WorkflowStep, WorkflowEvent):
                    session.execute(delete(model).where(model.workflow_id == workflow_id))
                session.execute(delete(Workflow).where(Workflow.id == workflow_id))


def task_count(workflow_id):
    from app.persistence.database import engine

    with Session(engine) as session:
        return session.scalar(
            select(func.count())
            .select_from(FollowupTask)
            .where(FollowupTask.workflow_id == workflow_id)
        )


def request_approval(client, path):
    response = client.post(path + "/run", params={"plan": "support_followup"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "WAITING_FOR_APPROVAL"

    response = client.get(path + "/events")
    assert response.status_code == 200, response.text
    event = response.json()[-1]
    assert event["to_status"] == "WAITING_FOR_APPROVAL"
    assert event["details"]["ticket_ids"] == ["T-102", "T-104", "T-101"]
    return event["details"]["plan_hash"]


@pytest.mark.parametrize(
    "decision, resume_code, final_status, expected_tasks",
    [
        ("approve", 200, "COMPLETED", 3),
        ("reject", 409, "CANCELLED", 0),
    ],
)
def test_approval_is_required_and_bound_to_plan(
    scenario, decision, resume_code, final_status, expected_tasks
):
    client, path, workflow_id, headers = scenario
    digest = request_approval(client, path)
    decision_path = f"{path}/{decision}"

    repeated_run = client.post(path + "/run")
    assert repeated_run.status_code == 200, repeated_run.text
    assert repeated_run.json()["status"] == "WAITING_FOR_APPROVAL"
    assert task_count(workflow_id) == 0
    assert client.post(decision_path, json={"plan_hash": digest}).status_code == 403

    wrong_digest = ("0" if digest[0] != "0" else "1") + digest[1:]
    response = client.post(decision_path, json={"plan_hash": wrong_digest}, headers=headers)
    assert response.status_code == 409, response.text
    assert task_count(workflow_id) == 0

    response = client.post(decision_path, json={"plan_hash": digest}, headers=headers)
    assert response.status_code == 200, response.text
    assert task_count(workflow_id) == 0

    resumed = client.post(path + "/run")
    assert resumed.status_code == resume_code, resumed.text
    assert client.get(path).json()["status"] == final_status
    assert task_count(workflow_id) == expected_tasks


def test_concurrent_resume_creates_one_task_per_ticket(scenario, monkeypatch):
    from app.api import workflows as workflows_api
    from app.persistence.database import engine

    client, path, workflow_id, headers = scenario
    digest = request_approval(client, path)
    response = client.post(path + "/approve", json={"plan_hash": digest}, headers=headers)
    assert response.status_code == 200, response.text
    assert task_count(workflow_id) == 0

    write_flushed = Event()
    second_run_entered = Event()
    release_write = Event()
    write_calls = []
    real_tool = SUPPORT_TOOLS["create_followup_tasks"]
    real_execute = workflows_api.execute_workflow

    def pause_after_flush(session, tool_workflow_id, inputs, arguments):
        write_calls.append(tool_workflow_id)
        output = real_tool(session, tool_workflow_id, inputs, arguments)
        session.flush()
        write_flushed.set()
        assert release_write.wait(15), "Timed out waiting to release the task write"
        return output

    def observe_run(*args, **kwargs):
        if write_flushed.is_set():
            second_run_entered.set()
        return real_execute(*args, **kwargs)

    monkeypatch.setitem(SUPPORT_TOOLS, "create_followup_tasks", pause_after_flush)
    monkeypatch.setattr(workflows_api, "execute_workflow", observe_run)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(client.post, path + "/run")
        try:
            assert write_flushed.wait(10), "First Resume did not flush the task rows"
            second = pool.submit(client.post, path + "/run")
            assert second_run_entered.wait(10), "Second Resume did not reach the engine"
        finally:
            release_write.set()
        responses = [first.result(timeout=15), second.result(timeout=15)]

    assert all(response.status_code in (200, 409) for response in responses), [
        (response.status_code, response.text) for response in responses
    ]
    assert any(
        response.status_code == 200 and response.json()["status"] == "COMPLETED"
        for response in responses
    )
    assert client.get(path).json()["status"] == "COMPLETED"
    assert write_calls == [workflow_id]

    with Session(engine) as session:
        ticket_ids = session.scalars(
            select(FollowupTask.ticket_id)
            .where(FollowupTask.workflow_id == workflow_id)
            .order_by(FollowupTask.ticket_id)
        ).all()
    assert list(ticket_ids) == ["T-101", "T-102", "T-104"]

    response = client.get(path + "/events")
    assert response.status_code == 200, response.text
    assert sum(event["to_status"] == "COMPLETED" for event in response.json()) == 1


@pytest.mark.parametrize(
    "error_type, failures, expected_attempts, final_status, expected_waits",
    [
        (RetryableToolError, 3, 4, "COMPLETED", [1, 2, 4]),
        (RetryableToolError, 4, 4, "FAILED", [1, 2, 4]),
        (PermanentToolError, 1, 1, "FAILED", []),
    ],
)
def test_tool_retries_preserve_atomic_task_writes(
    scenario, monkeypatch, error_type, failures, expected_attempts, final_status, expected_waits
):
    client, path, workflow_id, headers = scenario
    digest = request_approval(client, path)
    response = client.post(path + "/approve", json={"plan_hash": digest}, headers=headers)
    assert response.status_code == 200, response.text

    waits = []
    now = datetime(2026, 1, 1, tzinfo=UTC)

    def advance(seconds):
        nonlocal now
        waits.append(seconds)
        now += timedelta(seconds=seconds)

    monkeypatch.setattr(workflow_engine, "datetime", SimpleNamespace(now=lambda tz: now.astimezone(tz)))
    monkeypatch.setattr(workflow_engine, "sleep", advance)

    real_tool = SUPPORT_TOOLS["create_followup_tasks"]
    attempts = 0

    def inject_failure(session, tool_workflow_id, inputs, arguments):
        nonlocal attempts
        attempts += 1
        output = real_tool(session, tool_workflow_id, inputs, arguments)
        session.flush()
        if attempts <= failures:
            raise error_type("Injected tool failure")
        return output

    monkeypatch.setitem(SUPPORT_TOOLS, "create_followup_tasks", inject_failure)

    response = client.post(path + "/run")
    assert response.status_code == (200 if final_status == "COMPLETED" else 500), response.text
    assert client.get(path).json()["status"] == final_status
    assert attempts == expected_attempts
    assert waits == expected_waits
    assert task_count(workflow_id) == (3 if final_status == "COMPLETED" else 0)

    response = client.get(path + "/events")
    assert response.status_code == 200, response.text
    retry_counts = [
        event["details"]["failure_count"]
        for event in response.json()
        if event["to_status"] == "RETRYING"
    ]
    assert retry_counts == list(range(1, len(expected_waits) + 1))


def ollama_response(content, done_reason="stop"):
    return dumps({
        "done": True,
        "done_reason": done_reason,
        "message": {"content": content},
    }).encode("utf-8")


@pytest.mark.parametrize(
    "ollama_body",
    [
        pytest.param(b'{"done":', id="invalid-http-json"),
        pytest.param(ollama_response('{"action":'), id="invalid-intent-json"),
        pytest.param(
            ollama_response('{"action":"list_tickets","limit":3}', done_reason="length"),
            id="truncated-intent",
        ),
        pytest.param(
            ollama_response('{"action":"send_email","limit":3}'), id="unknown-action"
        ),
        pytest.param(
            ollama_response('{"action":"create_followup_tasks","limit":11}'),
            id="out-of-range-limit",
        ),
        pytest.param(
            ollama_response('{"action":"create_followup_tasks"}'), id="missing-limit"
        ),
    ],
)
def test_invalid_ollama_response_cannot_execute(scenario, monkeypatch, ollama_body):
    from app.persistence.database import engine
    from app.planner import ollama_planner

    client, path, workflow_id, _ = scenario
    ollama_request = Mock(side_effect=lambda *args, **kwargs: BytesIO(ollama_body))
    monkeypatch.setattr(ollama_planner, "urlopen", ollama_request)

    unexpected_tool = Mock(side_effect=AssertionError("Invalid plan reached a support tool"))
    for tool_name in SUPPORT_TOOLS:
        monkeypatch.setitem(SUPPORT_TOOLS, tool_name, unexpected_tool)

    response = client.post(path + "/plan")
    assert response.status_code == 502, response.text
    ollama_request.assert_called_once()

    response = client.post(path + "/run", params={"plan": "generated"})
    assert 400 <= response.status_code < 500, response.text
    unexpected_tool.assert_not_called()

    with Session(engine) as session:
        workflow = session.get(Workflow, workflow_id)
        assert workflow is not None
        assert workflow.status == "PENDING"
        assert workflow.generated_plan is None
        assert workflow.execution_plan is None
        assert session.scalar(
            select(func.count())
            .select_from(WorkflowStep)
            .where(WorkflowStep.workflow_id == workflow_id)
        ) == 0

    assert task_count(workflow_id) == 0
