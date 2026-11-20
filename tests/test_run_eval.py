import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def runner(monkeypatch):
    source = Path(__file__).resolve().parents[1] / "scripts" / "run_eval.py"
    spec = importlib.util.spec_from_file_location("axon_run_eval", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(module, "perf_counter", lambda: clock.now)
    monkeypatch.setattr(module, "sleep", lambda seconds: setattr(clock, "now", clock.now + seconds))
    return module, clock


def exchanges(monkeypatch, runner, responses, observer=None):
    module, clock = runner
    pending = iter(responses)
    calls = []

    def request(_base_url, path, *, method="GET", **kwargs):
        expected_method, expected_path, code, body = next(pending)
        assert (method, path) == (expected_method, expected_path)
        assert kwargs.get("timeout", 200) > 0
        if observer is not None:
            observer(method, path, kwargs)
        calls.append((method, path))
        clock.now += 0.1
        return code, body

    monkeypatch.setattr(module, "request_json", request)
    return calls


CASE = {
    "id": "read-one", "category": "read", "request": "Show one ticket",
    "expect": {
        "tools": ["list_tickets"], "limit": 1,
        "status": "COMPLETED", "ticket_ids": ["T-102"],
    },
}
PLAN = {"steps": [{
    "name": "find_tickets", "tool": "list_tickets",
    "arguments": {"limit": 1}, "depends_on": [],
}]}
CREATE = ("POST", "/workflows", 201, {"id": "workflow-1"})
JOB = "/workflows/workflow-1/plan-job"
RUN = ("POST", "/workflows/workflow-1/run?plan=generated", 200, {"status": "COMPLETED"})
EVENTS = ("GET", "/workflows/workflow-1/events", 200, [{
    "to_status": "COMPLETED", "details": {"tickets": [{"id": "T-102"}]},
}])


def test_job_recovery_keeps_failure_and_total_wait_visible(runner, monkeypatch):
    module, _ = runner
    calls = exchanges(monkeypatch, runner, [
        CREATE,
        ("POST", JOB, 202, {"status": "planning", "attempts": 0}),
        ("GET", JOB, 200, {
            "status": "planning", "attempts": 1, "last_error_code": "ollama_timeout",
        }),
        ("GET", JOB, 200, {
            "status": "ready", "plan": PLAN, "attempts": 2,
            "last_error_code": "ollama_timeout", "duration_ms": 5,
        }),
        RUN, EVENTS,
    ])
    result = module.evaluate_case("http://axon", CASE, planning_mode="job", poll_interval=1)
    assert result["passed"] is True
    assert result["workflow_id"] == "workflow-1"
    assert result["plan_latency_ms"] >= 2000
    assert result["actual"]["plan_job_attempts"] == 2
    assert result["actual"]["plan_job_last_error_code"] == "ollama_timeout"
    assert result["actual"]["plan_job_duration_ms"] == 5
    assert "plan_http_status" not in result["actual"]
    assert calls.count(("POST", JOB)) == 1


@pytest.mark.parametrize("reason, expected_pass", [
    ("unsupported", True), ("needs_clarification", False),
    ("ollama_timeout", False), ("planning_interrupted", False),
])
def test_job_failure_is_scored_by_error_code(runner, monkeypatch, reason, expected_pass):
    module, _ = runner
    calls = exchanges(monkeypatch, runner, [
        CREATE,
        ("POST", JOB, 202, {"status": "planning"}),
        ("GET", JOB, 200, {
            "status": "failed", "error_code": reason,
            "error": "Human-readable text is not the machine error code", "attempts": 1,
        }),
    ])
    case = {**CASE, "expect": {"rejection": "unsupported"}}
    result = module.evaluate_case("http://axon", case, planning_mode="job")
    assert result["passed"] is expected_pass
    assert result["actual"]["plan_job_http_status"] == 200
    assert result["actual"]["plan_job_error_code"] == reason
    assert result["execution_passed"] is None
    assert len(calls) == 3


def test_poll_deadline_does_not_resubmit_or_execute_the_workflow(runner, monkeypatch):
    module, _ = runner
    calls = exchanges(monkeypatch, runner, [
        CREATE, ("POST", JOB, 202, {"status": "planning", "attempts": 0}),
    ])
    result = module.evaluate_case(
        "http://axon", CASE, planning_mode="job", planning_timeout=0.5, poll_interval=2,
    )
    assert result["passed"] is False
    assert result["actual"]["plan_job_poll_timed_out"] is True
    assert result["plan_latency_ms"] == 500
    assert calls == [("POST", "/workflows"), ("POST", JOB)]


def test_ready_response_after_deadline_does_not_count_as_success(runner, monkeypatch):
    module, _ = runner
    exchanges(monkeypatch, runner, [
        CREATE, ("POST", JOB, 200, {"status": "ready", "plan": PLAN, "attempts": 1}),
    ])
    result = module.evaluate_case(
        "http://axon", CASE, planning_mode="job", planning_timeout=0.05,
    )
    assert result["passed"] is False
    assert result["actual"]["plan_job_poll_timed_out"] is True
    assert result["execution_passed"] is None


@pytest.mark.parametrize("http_code", [0, 403, 500])
def test_poll_http_failure_is_not_treated_as_a_plan(runner, monkeypatch, http_code):
    module, _ = runner
    exchanges(monkeypatch, runner, [
        CREATE, ("POST", JOB, 202, {"status": "planning"}),
        ("GET", JOB, http_code, {"detail": "Unavailable"}),
    ])
    result = module.evaluate_case("http://axon", CASE, planning_mode="job")
    assert result["passed"] is False
    assert result["actual"]["plan_job_http_status"] == http_code
    assert result["actual"]["detail"] == "Unavailable"
    assert result["execution_passed"] is None


def test_sync_mode_preserves_single_call_timeout_failure(runner, monkeypatch):
    module, _ = runner
    calls = exchanges(monkeypatch, runner, [
        CREATE, ("POST", "/workflows/workflow-1/plan", 502, {"detail": "Ollama timed out"}),
    ])
    result = module.evaluate_case("http://axon", CASE)
    assert result["passed"] is False
    assert result["actual"]["plan_http_status"] == 502
    assert len(calls) == 2


def test_job_cli_writes_a_failed_report_and_returns_nonzero(runner, monkeypatch, tmp_path):
    module, _ = runner
    dataset = tmp_path / "cases.jsonl"
    dataset.write_text(json.dumps(CASE) + "\n")
    output = tmp_path / "report.json"
    exchanges(monkeypatch, runner, [
        ("GET", "/health", 200, {"status": "ok"}), CREATE,
        ("POST", JOB, 202, {"status": "planning"}),
        ("GET", JOB, 200, {
            "status": "failed", "attempts": 3, "error_code": "ollama_timeout",
        }),
    ])
    monkeypatch.setattr("sys.argv", [
        "run_eval", "--planning-mode", "job", "--dataset", str(dataset),
        "--output", str(output), "--fail-on-regression",
    ])
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 1
    report = json.loads(output.read_text())
    assert report["summary"]["planning_mode"] == "job"
    assert report["summary"]["planning_jobs_retried"] == 1
    assert report["summary"]["planning_jobs_recovered"] == 0
    assert "plan_job_latency_ms" in report["summary"]
    assert "plan_http_latency_ms" not in report["summary"]


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf"])
def test_wait_settings_reject_nonpositive_or_infinite_values(runner, value):
    module, _ = runner
    with pytest.raises(module.argparse.ArgumentTypeError):
        module.positive_seconds(value)


FOLLOWUP_CASE = {
    "id": "followup-two", "category": "approval", "request": "Create two follow-up tasks",
    "expect": {
        "tools": ["list_tickets", "summarize_tickets", "create_followup_tasks"],
        "limit": 2, "status": "WAITING_FOR_APPROVAL", "ticket_ids": ["T-102", "T-104"],
    },
}
FOLLOWUP_PLAN = {"steps": [
    {"name": "find_tickets", "tool": "list_tickets", "arguments": {"limit": 2}, "depends_on": []},
    {"name": "summarize_tickets", "tool": "summarize_tickets", "arguments": {}, "depends_on": ["find_tickets"]},
    {"name": "create_tasks", "tool": "create_followup_tasks", "arguments": {}, "depends_on": ["summarize_tickets"]},
]}


def followup_fixture():
    from hashlib import sha256

    digest = sha256(json.dumps(FOLLOWUP_PLAN, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    waiting = {
        "to_status": "WAITING_FOR_APPROVAL", "details": {
            "plan_hash": digest, "step": "create_tasks", "tool": "create_followup_tasks",
            "task_count": 2, "ticket_ids": ["T-102", "T-104"],
        },
    }
    tasks = [
        {"ticket_id": "T-102", "summary": "Priority 5: Checkout unavailable"},
        {"ticket_id": "T-104", "summary": "Priority 4: Account access blocked"},
    ]
    events = [waiting, {
        "to_status": "RUNNING", "details": {
            "plan_hash": digest, "step": "create_tasks", "decision": "approved",
        },
    }, {
        "to_status": "COMPLETED", "details": {
            "tickets": [{"id": "T-102"}, {"id": "T-104"}], "followup_tasks": tasks,
        },
    }]
    return waiting, tasks, events


def followup_prefix(waiting=None):
    if waiting is None:
        waiting, _, _ = followup_fixture()
    return [
        CREATE,
        ("POST", "/workflows/workflow-1/plan", 200, FOLLOWUP_PLAN),
        ("POST", "/workflows/workflow-1/run?plan=generated", 200, {"status": "WAITING_FOR_APPROVAL"}),
        ("GET", "/workflows/workflow-1/events", 200, [waiting]),
    ]


def full_followup_responses(tasks=None, events=None, persisted_status="COMPLETED"):
    _, expected_tasks, expected_events = followup_fixture()
    return followup_prefix() + [
        ("GET", "/workflows/workflow-1/tasks", 200, []),
        ("POST", "/workflows/workflow-1/approve", 200, {"status": "RUNNING"}),
        ("GET", "/workflows/workflow-1/tasks", 200, []),
        ("POST", "/workflows/workflow-1/run?plan=generated", 200, {"status": "COMPLETED"}),
        ("GET", "/workflows/workflow-1", 200, {"status": persisted_status}),
        ("GET", "/workflows/workflow-1/events", 200, expected_events if events is None else events),
        ("GET", "/workflows/workflow-1/tasks", 200, expected_tasks if tasks is None else tasks),
    ]


def test_default_followup_evaluation_stops_at_approval(runner, monkeypatch):
    module, _ = runner
    calls = exchanges(monkeypatch, runner, followup_prefix())
    result = module.evaluate_case("http://axon", FOLLOWUP_CASE, approval_token="unused-secret")
    assert result["passed"] is True
    assert result["actual"]["status"] == "WAITING_FOR_APPROVAL"
    assert len(calls) == 4


def test_complete_followup_verifies_persisted_tasks_and_scopes_the_token(runner, monkeypatch):
    module, _ = runner
    observed = []
    _, tasks, _ = followup_fixture()
    exchanges(monkeypatch, runner, full_followup_responses(tasks=tasks[::-1]),
              observer=lambda method, path, kwargs: observed.append((method, path, kwargs)))
    result = module.evaluate_case(
        "http://axon", FOLLOWUP_CASE, approve_followups=True, approval_token="private-operator-token",
    )
    assert result["passed"] is True
    assert result["approval_wait_passed"] is True
    assert result["approval_passed"] is True
    assert result["tasks_verified"] is True
    assert result["actual"]["initial_status"] == "WAITING_FOR_APPROVAL"
    assert result["actual"]["persisted_status"] == "COMPLETED"
    assert result["actual"]["followup_tasks"] == tasks
    assert result["actual"]["matching_approval_events"] == 1
    authenticated = [(method, path, kwargs) for method, path, kwargs in observed if kwargs.get("headers")]
    assert len(authenticated) == 1
    method, path, kwargs = authenticated[0]
    assert (method, path) == ("POST", "/workflows/workflow-1/approve")
    assert kwargs["headers"] == {"X-Axon-Approval-Token": "private-operator-token"}
    waiting, _, _ = followup_fixture()
    assert kwargs["payload"] == {"plan_hash": waiting["details"]["plan_hash"]}
    assert "private-operator-token" not in json.dumps(result)


@pytest.mark.parametrize("field, value", [
    ("plan_hash", "0" * 64), ("task_count", 1), ("step", "another_write"),
])
def test_unmatched_approval_is_never_sent(runner, monkeypatch, field, value):
    module, _ = runner
    waiting, _, _ = followup_fixture()
    waiting["details"][field] = value
    calls = exchanges(monkeypatch, runner, followup_prefix(waiting))
    result = module.evaluate_case("http://axon", FOLLOWUP_CASE, approve_followups=True, approval_token="secret")
    assert result["passed"] is False
    assert result["actual"]["completion_error"] == "approval_plan_mismatch"
    assert len(calls) == 4


def test_tasks_before_approval_prevent_approval(runner, monkeypatch):
    module, _ = runner
    _, tasks, _ = followup_fixture()
    calls = exchanges(monkeypatch, runner, followup_prefix() + [
        ("GET", "/workflows/workflow-1/tasks", 200, tasks),
    ])
    result = module.evaluate_case("http://axon", FOLLOWUP_CASE, approve_followups=True, approval_token="secret")
    assert result["passed"] is False
    assert result["tasks_verified"] is False
    assert result["actual"]["completion_error"] == "tasks_not_empty_before_approval"
    assert len(calls) == 5


def test_denied_approval_does_not_resume_or_report_the_response_body(runner, monkeypatch):
    module, _ = runner
    calls = exchanges(monkeypatch, runner, followup_prefix() + [
        ("GET", "/workflows/workflow-1/tasks", 200, []),
        ("POST", "/workflows/workflow-1/approve", 403, {"detail": "echoed-secret"}),
    ])
    result = module.evaluate_case("http://axon", FOLLOWUP_CASE, approve_followups=True, approval_token="echoed-secret")
    assert result["passed"] is False
    assert result["approval_passed"] is False
    assert result["actual"]["approval_http_status"] == 403
    assert "echoed-secret" not in json.dumps(result)
    assert len(calls) == 6


def test_tasks_created_by_approval_prevent_resume(runner, monkeypatch):
    module, _ = runner
    _, tasks, _ = followup_fixture()
    responses = full_followup_responses()[:7]
    responses[-1] = ("GET", "/workflows/workflow-1/tasks", 200, tasks)
    calls = exchanges(monkeypatch, runner, responses)
    result = module.evaluate_case("http://axon", FOLLOWUP_CASE, approve_followups=True, approval_token="secret")
    assert result["passed"] is False
    assert result["approval_passed"] is True
    assert result["actual"]["completion_error"] == "tasks_not_empty_before_resume"
    assert len(calls) == 7


@pytest.mark.parametrize("change", ["missing", "extra", "duplicate", "wrong-summary", "blank-summary"])
def test_completed_event_cannot_hide_incorrect_task_rows(runner, monkeypatch, change):
    module, _ = runner
    _, tasks, _ = followup_fixture()
    if change == "missing":
        tasks = tasks[:1]
    elif change == "extra":
        tasks.append({"ticket_id": "T-101", "summary": "Unexpected task"})
    elif change == "duplicate":
        tasks = [tasks[0], tasks[0]]
    else:
        tasks[0]["summary"] = "Different content" if change == "wrong-summary" else " "
    exchanges(monkeypatch, runner, full_followup_responses(tasks=tasks))
    result = module.evaluate_case("http://axon", FOLLOWUP_CASE, approve_followups=True, approval_token="secret")
    assert result["passed"] is False
    assert result["tasks_verified"] is False
    assert result["actual"]["completion_error"] == "completion_verification_failed"


def test_completed_rows_require_a_matching_approval_audit_event(runner, monkeypatch):
    module, _ = runner
    _, _, events = followup_fixture()
    events[1]["details"]["plan_hash"] = "0" * 64
    exchanges(monkeypatch, runner, full_followup_responses(events=events))
    result = module.evaluate_case("http://axon", FOLLOWUP_CASE, approve_followups=True, approval_token="secret")
    assert result["tasks_verified"] is True
    assert result["passed"] is False
    assert result["actual"]["matching_approval_events"] == 0


def test_completed_response_requires_persisted_completed_status(runner, monkeypatch):
    module, _ = runner
    exchanges(monkeypatch, runner, full_followup_responses(persisted_status="RUNNING"))
    result = module.evaluate_case("http://axon", FOLLOWUP_CASE, approve_followups=True, approval_token="secret")
    assert result["tasks_verified"] is True
    assert result["passed"] is False


def test_resume_failure_does_not_count_as_execution_success(runner, monkeypatch):
    module, _ = runner
    responses = full_followup_responses()[:8]
    responses[-1] = ("POST", "/workflows/workflow-1/run?plan=generated", 500, {"detail": "Failed"})
    calls = exchanges(monkeypatch, runner, responses)
    result = module.evaluate_case("http://axon", FOLLOWUP_CASE, approve_followups=True, approval_token="secret")
    assert result["passed"] is False
    assert result["execution_passed"] is False
    assert result["actual"]["completion_error"] == "resume_failed"
    assert len(calls) == 8


def test_completion_cli_requires_token_before_any_requests(runner, monkeypatch):
    module, _ = runner
    monkeypatch.delenv("AXON_APPROVAL_TOKEN", raising=False)
    monkeypatch.setattr("sys.argv", ["run_eval", "--approve-followups"])
    monkeypatch.setattr(module, "request_json", lambda *args, **kwargs: pytest.fail("Unexpected API request"))
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 2


def test_completion_cli_redacts_token_from_console_and_report(runner, monkeypatch, tmp_path, capsys):
    module, _ = runner
    secret = 'private-operator-"token\\value'
    dataset = tmp_path / "cases.jsonl"
    dataset.write_text(json.dumps({**CASE, "id": "case-" + secret}) + "\n")
    output = tmp_path / "report.json"
    monkeypatch.setenv("AXON_APPROVAL_TOKEN", secret)
    monkeypatch.setattr("sys.argv", [
        "run_eval", "--approve-followups", "--dataset", str(dataset), "--output", str(output),
    ])
    exchanges(monkeypatch, runner, [
        ("GET", "/health", 200, {"status": "ok"}), CREATE,
        ("POST", "/workflows/workflow-1/plan", 502, {"detail": "Echo " + secret}),
    ])
    module.main()
    report = json.loads(output.read_text())
    assert secret not in capsys.readouterr().out
    assert report["cases"][0]["actual"]["detail"] == "Echo [REDACTED]"
    assert report["cases"][0]["id"] == "case-[REDACTED]"
    assert report["summary"]["execution_scope"] == "completed_with_persisted_followups"


def test_credentialed_requests_do_not_follow_redirects(runner):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    module, _ = runner
    paths = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            paths.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/redirected")
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def do_GET(self):
            paths.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        code, _ = module.request_json(
            f"http://127.0.0.1:{server.server_port}", "/approve", method="POST",
            payload={}, headers={"X-Axon-Approval-Token": "private-token"},
        )
        assert code == 302
        assert paths == ["/approve"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_invalid_credential_header_error_does_not_return_its_value(runner, monkeypatch):
    module, _ = runner

    def invalid_header(*_args, **_kwargs):
        raise ValueError("Invalid header: private-token")

    monkeypatch.setattr(module, "build_opener", lambda *_args: SimpleNamespace(open=invalid_header))
    code, body = module.request_json(
        "http://axon", "/approve", method="POST", payload={},
        headers={"X-Axon-Approval-Token": "private-token"},
    )
    assert code == 0
    assert body == {"detail": "ValueError"}


@pytest.mark.parametrize("complete", [True, False], ids=["completed", "missing-task"])
def test_completion_cli_rates_include_task_verification(runner, monkeypatch, tmp_path, complete):
    module, _ = runner
    dataset = tmp_path / "followups.jsonl"
    dataset.write_text(json.dumps(FOLLOWUP_CASE) + "\n")
    output = tmp_path / "report.json"
    _, tasks, _ = followup_fixture()
    responses = full_followup_responses(tasks=tasks if complete else tasks[:1])
    exchanges(monkeypatch, runner, [("GET", "/health", 200, {"status": "ok"}), *responses])
    monkeypatch.setenv("AXON_APPROVAL_TOKEN", "private-token")
    monkeypatch.setattr("sys.argv", [
        "run_eval", "--approve-followups", "--dataset", str(dataset),
        "--output", str(output), "--fail-on-regression",
    ])
    if complete:
        module.main()
    else:
        with pytest.raises(SystemExit) as error:
            module.main()
        assert error.value.code == 1
    report = json.loads(output.read_text())
    summary = report["summary"]
    assert summary["execution_scope"] == "completed_with_persisted_followups"
    assert summary["execution_end_to_end_pass_rate"] == (1.0 if complete else 0.0)
    assert summary["approval_checks"] == summary["approval_passed"] == 1
    assert summary["task_verification_checks"] == 1
    assert summary["task_verification_passed"] == int(complete)
    assert report["cases"][0]["approval_wait_passed"] is True
    assert report["cases"][0]["passed"] is complete
