from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from io import BytesIO
from json import dumps
from os import getenv
from secrets import token_urlsafe
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.error import URLError
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


@pytest.mark.parametrize(
    "action, opposite_action, decision, expected_status",
    [
        ("approve", "reject", "approved", "RUNNING"),
        ("reject", "approve", "rejected", "CANCELLED"),
    ],
)
def test_approval_replay_preserves_decision_and_audit(
    scenario, action, opposite_action, decision, expected_status,
):
    from app.persistence.database import engine
    from app.persistence.models import WorkflowApproval

    client, path, workflow_id, headers = scenario
    digest = request_approval(client, path)
    decision_path = f"{path}/{action}"
    payload = {"plan_hash": digest}

    def approval_record():
        with Session(engine) as session:
            approval = session.get(WorkflowApproval, (workflow_id, digest))
            assert approval is not None
            return approval.decision, approval.decided_at, approval.step_name

    first = client.post(decision_path, json=payload, headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == expected_status
    saved_record = approval_record()
    assert saved_record[0] == decision
    assert saved_record[1] is not None

    response = client.get(path + "/events")
    assert response.status_code == 200, response.text
    original_events = response.json()
    decisions = [event for event in original_events if event["details"].get("decision")]
    assert len(decisions) == 1
    event = decisions[0]
    assert event["from_status"] == "WAITING_FOR_APPROVAL"
    assert event["to_status"] == expected_status
    assert event["details"]["plan_hash"] == digest
    assert event["details"]["step"] == saved_record[2]
    assert event["details"]["decision"] == decision

    replay = client.post(decision_path, json=payload, headers=headers)
    assert replay.status_code == 200, replay.text
    assert replay.json()["status"] == expected_status

    conflict = client.post(f"{path}/{opposite_action}", json=payload, headers=headers)
    assert conflict.status_code == 409, conflict.text

    denied = client.post(
        decision_path, json=payload, headers={"X-Axon-Approval-Token": "invalid-token"}
    )
    assert denied.status_code == 403, denied.text
    assert approval_record() == saved_record

    response = client.get(path + "/events")
    assert response.status_code == 200, response.text
    assert response.json() == original_events

    workflow = client.get(path)
    assert workflow.status_code == 200, workflow.text
    assert workflow.json()["status"] == expected_status
    assert task_count(workflow_id) == 0


def test_revoked_tool_policy_blocks_approved_write(scenario, monkeypatch):
    from app.persistence.database import engine
    from app.validation import policy

    client, path, workflow_id, headers = scenario
    digest = request_approval(client, path)
    approved = client.post(path + "/approve", json={"plan_hash": digest}, headers=headers)
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "RUNNING"
    assert task_count(workflow_id) == 0

    tool = "create_followup_tasks"
    execute_step = workflow_engine._execute_step
    unexpected_write = Mock(side_effect=AssertionError("A denied tool was called"))
    reached_write_step = False

    with monkeypatch.context() as revoke:
        def revoke_before_dispatch(session, workflow_id, step, position, inputs, plan_hash):
            nonlocal reached_write_step
            if step.tool == tool:
                reached_write_step = True
                revoke.setitem(policy._TOOL_POLICY, tool, policy.ToolDecision.DENY)
            return execute_step(session, workflow_id, step, position, inputs, plan_hash)

        revoke.setitem(SUPPORT_TOOLS, tool, unexpected_write)
        revoke.setattr(workflow_engine, "_execute_step", revoke_before_dispatch)
        denied = client.post(path + "/run")

    assert reached_write_step
    assert 400 <= denied.status_code < 500, denied.text
    assert denied.json()["detail"] == "Tool execution is blocked by policy"
    unexpected_write.assert_not_called()
    assert task_count(workflow_id) == 0

    with Session(engine) as session:
        checkpoint = session.scalar(
            select(WorkflowStep.name).where(
                WorkflowStep.workflow_id == workflow_id, WorkflowStep.tool == tool
            )
        )
        assert checkpoint is None

    resumed = client.post(path + "/run")
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "COMPLETED"
    assert task_count(workflow_id) == 3


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



def test_workflow_list_filters_pending_approvals(scenario):
    client, path, workflow_id, headers = scenario
    digest = request_approval(client, path)
    filters = {"status": "WAITING_FOR_APPROVAL", "limit": 100}

    response = client.get("/workflows", params=filters)
    assert response.status_code == 200, response.text
    records = response.json()["items"]
    assert any(row["id"] == str(workflow_id) for row in records)
    assert all(row["status"] == "WAITING_FOR_APPROVAL" for row in records)

    response = client.post(path + "/reject", json={"plan_hash": digest}, headers=headers)
    assert response.status_code == 200, response.text

    response = client.get("/workflows", params=filters)
    assert response.status_code == 200, response.text
    assert all(row["id"] != str(workflow_id) for row in response.json()["items"])
    assert client.get("/workflows", params={"status": "INVALID"}).status_code == 422


@pytest.mark.parametrize("route", ["/plan", "/plan-job"])
@pytest.mark.parametrize(
    "failure, error_code",
    [
        pytest.param(TimeoutError("upstream timeout"), "ollama_timeout", id="timeout"),
        pytest.param(
            URLError(TimeoutError("upstream timeout")), "ollama_timeout", id="wrapped-timeout"
        ),
        pytest.param(
            URLError(ConnectionRefusedError("connection refused")),
            "ollama_connection_error",
            id="connection-refused",
        ),
        pytest.param(None, "ollama_invalid_response", id="invalid-response"),
    ],
)
def test_planning_failure_logs_reason(scenario, monkeypatch, route, failure, error_code):
    from app.planner import jobs
    from app.planner import ollama_planner

    client, path, workflow_id, _ = scenario

    def fake_ollama(*args, **kwargs):
        if failure is not None:
            raise failure
        return BytesIO(b'{"done":')

    monkeypatch.setattr(ollama_planner, "urlopen", fake_ollama)
    log = Mock(wraps=jobs.log_event)
    monkeypatch.setattr(jobs, "log_event", log)

    response = client.post(path + route)
    if route == "/plan":
        assert response.status_code == 502, response.text
    else:
        assert response.status_code == 202, response.text
        claim = jobs.claim_plan_job(workflow_id)
        assert claim is not None
        jobs.run_plan_job(claim)
        job = client.get(path + route)
        assert job.status_code == 200, job.text
        expected_status = "planning" if error_code == "ollama_timeout" else "failed"
        assert job.json()["status"] == expected_status, job.text

    failures = [entry.kwargs for entry in log.call_args_list if entry.args == ("planning.failed",)]
    assert failures == [{"workflow_id": str(workflow_id), "error_code": error_code}]


def _expire_planning_lease(workflow_id):
    from app.persistence.database import engine

    with Session(engine) as session, session.begin():
        workflow = session.get(Workflow, workflow_id, with_for_update=True)
        assert workflow is not None
        workflow.plan_job_run_at = datetime(2000, 1, 1, tzinfo=UTC)


def test_planning_job_recovers_after_worker_crash(scenario):
    from subprocess import run
    from sys import executable
    from textwrap import dedent

    from app.persistence.database import engine
    from app.planner import jobs

    client, path, workflow_id, _ = scenario
    response = client.post(path + "/plan-job")
    assert response.status_code == 202, response.text
    assert client.get(path + "/plan-job").json()["status"] == "planning"

    worker_script = dedent("""
        import os
        import sys
        from uuid import UUID

        from app.planner import jobs
        from app.validation.plan import SupportAction, build_support_plan

        def generate(_request):
            if sys.argv[2] == "crash":
                os._exit(73)
            return build_support_plan(SupportAction.LIST, 2)

        jobs.generate_plan = generate
        claim = jobs.claim_plan_job(UUID(sys.argv[1]))
        assert claim is not None
        jobs.run_plan_job(claim)
    """)
    first = run(
        [executable, "-c", worker_script, str(workflow_id), "crash"],
        capture_output=True, text=True, timeout=20,
    )
    assert first.returncode == 73, first.stderr
    assert jobs.claim_plan_job(workflow_id) is None

    with Session(engine) as session:
        interrupted = session.get(Workflow, workflow_id)
        assert interrupted is not None and interrupted.generated_plan is None
        assert interrupted.plan_job["attempts"] == 1
        job_id = interrupted.plan_job["id"]

    _expire_planning_lease(workflow_id)
    second = run(
        [executable, "-c", worker_script, str(workflow_id), "recover"],
        capture_output=True, text=True, timeout=20,
    )
    assert second.returncode == 0, second.stderr

    response = client.get(path + "/plan-job")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ready"
    assert [step["tool"] for step in response.json()["plan"]["steps"]] == ["list_tickets"]

    with Session(engine) as session:
        recovered = session.get(Workflow, workflow_id)
        assert recovered is not None
        assert recovered.plan_job["id"] == job_id
        assert recovered.plan_job_run_at is None

    repeated = client.post(path + "/plan-job")
    assert repeated.status_code == 200, repeated.text
    assert repeated.json() == response.json()


def test_expired_planning_attempt_cannot_save_after_reassignment(scenario, monkeypatch):
    from app.engine.workflow_engine import WorkflowRunConflict
    from app.persistence.database import engine
    from app.planner import jobs
    from app.validation.plan import SupportAction, build_support_plan

    client, path, workflow_id, _ = scenario
    assert client.post(path + "/plan-job").status_code == 202
    first = jobs.claim_plan_job(workflow_id)
    assert first is not None
    second = None

    def generate_after_reassignment(_request):
        nonlocal second
        _expire_planning_lease(workflow_id)
        second = jobs.claim_plan_job(workflow_id)
        assert second is not None
        assert second.job_id == first.job_id
        assert second.token != first.token
        return build_support_plan(SupportAction.LIST, 1)

    monkeypatch.setattr(jobs, "generate_plan", generate_after_reassignment)
    with Session(engine) as session:
        with pytest.raises(WorkflowRunConflict, match="expired"):
            jobs.generate_workflow_plan(workflow_id, session, first)

    assert second is not None
    assert client.get(path + "/plan-job").json()["status"] == "planning"
    monkeypatch.setattr(jobs, "generate_plan", lambda _: build_support_plan(SupportAction.LIST, 2))
    jobs.run_plan_job(second)
    result = client.get(path + "/plan-job").json()
    assert result["status"] == "ready"
    assert result["plan"]["steps"][0]["arguments"]["limit"] == 2


def test_interrupted_planning_stops_at_attempt_limit(scenario):
    from app.planner import jobs

    client, path, workflow_id, _ = scenario
    assert client.post(path + "/plan-job").status_code == 202
    for _ in range(jobs.MAX_ATTEMPTS):
        assert jobs.claim_plan_job(workflow_id) is not None
        _expire_planning_lease(workflow_id)

    assert jobs.claim_plan_job(workflow_id) is None
    result = client.get(path + "/plan-job").json()
    assert result["status"] == "failed"
    assert "interrupted" in result["error"]
    assert client.post(path + "/plan-job").status_code == 202
    assert jobs.claim_plan_job(workflow_id) is not None


@pytest.mark.parametrize("recover", [True, False], ids=["ready-on-retry", "exhausted"])
def test_worker_retries_ollama_timeout(scenario, monkeypatch, recover):
    from app.persistence.database import engine
    from app.planner import jobs
    from app.planner.ollama_planner import PlannerError
    from app.validation.plan import SupportAction, build_support_plan

    client, path, workflow_id, _ = scenario
    assert client.post(path + "/plan-job").status_code == 202
    monkeypatch.setattr(jobs, "TIMEOUT_RETRY_DELAY", timedelta(minutes=1))
    calls = 0

    def generate(_request):
        nonlocal calls
        calls += 1
        if recover and calls == 2:
            return build_support_plan(SupportAction.LIST, 2)
        raise PlannerError("Could not reach the local Ollama service", code="ollama_timeout")

    monkeypatch.setattr(jobs, "generate_plan", generate)
    expected_attempts = 2 if recover else jobs.MAX_ATTEMPTS
    job_id = None
    for attempt in range(1, expected_attempts + 1):
        claim = jobs.claim_plan_job(workflow_id)
        assert claim is not None
        job_id = job_id or claim.job_id
        assert claim.job_id == job_id
        jobs.run_plan_job(claim)
        if attempt < expected_attempts:
            assert client.get(path + "/plan-job").json()["status"] == "planning"
            assert client.post(path + "/plan-job").status_code == 202
            assert jobs.claim_plan_job(workflow_id) is None
            _expire_planning_lease(workflow_id)

    result = client.get(path + "/plan-job").json()
    assert result["status"] == ("ready" if recover else "failed")
    if recover:
        assert result["plan"]["steps"][0]["arguments"]["limit"] == 2
    else:
        assert "timed out" in result["error"]
    assert calls == expected_attempts

    with Session(engine) as session:
        workflow = session.get(Workflow, workflow_id)
        assert workflow is not None
        assert workflow.plan_job["id"] == job_id
        assert workflow.plan_job_run_at is None


@pytest.mark.parametrize(
    "invalid_output",
    [
        pytest.param([{"id": "T-102", "priority": "5", "issue": "Checkout"}], id="wrong-type"),
        pytest.param(
            [{"id": "T-102", "priority": 5, "issue": "Checkout", "extra": "unexpected"}],
            id="extra-field",
        ),
        pytest.param(
            [{"id": f"T-{index}", "priority": 1, "issue": "Test"} for index in range(4)],
            id="over-requested-limit",
        ),
    ],
)
def test_invalid_ticket_output_is_not_checkpointed(scenario, monkeypatch, invalid_output):
    from app.persistence.database import engine

    client, path, workflow_id, _ = scenario
    summarize = Mock(side_effect=AssertionError("Invalid tickets reached the next tool"))
    monkeypatch.setitem(SUPPORT_TOOLS, "list_tickets", lambda *args: invalid_output)
    monkeypatch.setitem(SUPPORT_TOOLS, "summarize_tickets", summarize)

    response = client.post(path + "/run", params={"plan": "support_followup"})
    assert response.status_code == 500, response.text
    assert client.get(path).json()["status"] == "FAILED"
    summarize.assert_not_called()
    assert task_count(workflow_id) == 0

    with Session(engine) as session:
        assert session.scalar(
            select(WorkflowStep.name).where(WorkflowStep.workflow_id == workflow_id)
        ) is None


def test_invalid_write_output_rolls_back_tasks(scenario, monkeypatch):
    from app.persistence.database import engine

    client, path, workflow_id, headers = scenario
    digest = request_approval(client, path)
    approved = client.post(path + "/approve", json={"plan_hash": digest}, headers=headers)
    assert approved.status_code == 200, approved.text
    real_write = SUPPORT_TOOLS["create_followup_tasks"]

    def write_with_invalid_output(session, workflow_id, inputs, arguments):
        inputs[0][0]["summary"] = "Different from the approved summary"
        output = real_write(session, workflow_id, inputs, arguments)
        session.flush()
        return output

    monkeypatch.setitem(SUPPORT_TOOLS, "create_followup_tasks", write_with_invalid_output)
    response = client.post(path + "/run")
    assert response.status_code == 500, response.text
    assert client.get(path).json()["status"] == "FAILED"
    assert task_count(workflow_id) == 0
    with Session(engine) as session:
        assert session.scalar(
            select(WorkflowStep.name).where(
                WorkflowStep.workflow_id == workflow_id,
                WorkflowStep.tool == "create_followup_tasks",
            )
        ) is None


def test_invalid_checkpoint_blocks_resume(scenario, monkeypatch):
    from app.persistence.database import engine

    client, path, workflow_id, headers = scenario
    digest = request_approval(client, path)
    approved = client.post(path + "/approve", json={"plan_hash": digest}, headers=headers)
    assert approved.status_code == 200, approved.text

    with Session(engine) as session, session.begin():
        checkpoint = session.scalar(
            select(WorkflowStep).where(
                WorkflowStep.workflow_id == workflow_id,
                WorkflowStep.tool == "summarize_tickets",
            )
        )
        assert checkpoint is not None
        checkpoint.output = [{"ticket_id": "T-102", "summary": False}]

    write = Mock(side_effect=AssertionError("Invalid checkpoint reached the write tool"))
    monkeypatch.setitem(SUPPORT_TOOLS, "create_followup_tasks", write)
    response = client.post(path + "/run")
    assert response.status_code == 409, response.text
    write.assert_not_called()
    assert task_count(workflow_id) == 0
