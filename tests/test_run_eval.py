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


def exchanges(monkeypatch, runner, responses):
    module, clock = runner
    pending = iter(responses)
    calls = []

    def request(_base_url, path, *, method="GET", **kwargs):
        expected_method, expected_path, code, body = next(pending)
        assert (method, path) == (expected_method, expected_path)
        assert kwargs.get("timeout", 200) > 0
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
