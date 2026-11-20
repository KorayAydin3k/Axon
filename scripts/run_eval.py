import argparse
import json
from hashlib import sha256
from math import ceil, isfinite
from os import getenv
from pathlib import Path
from statistics import median
from time import perf_counter, sleep
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen


DEFAULT_DATASET = Path(__file__).resolve().parents[1] / "evals" / "planner_cases.jsonl"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, _request, _response, _code, _message, _headers, _new_url):
        return None


def request_json(base_url, path, *, method="GET", payload=None, timeout=200, headers=None):
    try:
        request = Request(
            base_url + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json", **(headers or {})},
            method=method,
        )
        open_request = build_opener(NoRedirect()).open if headers else urlopen
        try:
            response = open_request(request, timeout=timeout)
        except HTTPError as error:
            response = error
        with response:
            return response.getcode(), json.load(response)
    except (URLError, TimeoutError, ValueError) as error:
        return 0, {"detail": type(error).__name__}


def canonical_steps(plan):
    by_name = {step["name"]: step["tool"] for step in plan["steps"]}
    return [
        {
            "tool": step["tool"],
            "arguments": step["arguments"],
            "depends_on": [by_name[name] for name in step["depends_on"]],
        }
        for step in plan["steps"]
    ]


def expected_steps(expected):
    tools = expected["tools"]
    return [
        {
            "tool": tool,
            "arguments": {"limit": expected["limit"]} if tool == "list_tickets" else {},
            "depends_on": tools[max(0, index - 1):index],
        }
        for index, tool in enumerate(tools)
    ]


def plan_with_job(base_url, path, *, timeout, poll_interval):
    actual = {}
    deadline = perf_counter() + timeout
    method = "POST"
    while True:
        remaining = deadline - perf_counter()
        if remaining <= 0:
            actual.update(
                plan_job_poll_timed_out=True,
                detail="Planning job wait deadline exceeded; the job may still be running",
            )
            return None, None, actual

        code, view = request_json(
            base_url, path + "/plan-job", method=method, timeout=min(10, remaining)
        )
        if method == "POST":
            actual["plan_job_start_http_status"] = code
        actual["plan_job_http_status"] = code
        if not isinstance(view, dict):
            actual["detail"] = "Invalid planning job response shape"
            return None, None, actual
        if code not in ((200, 202) if method == "POST" else (200,)):
            actual["detail"] = view.get("detail")
            return None, None, actual

        actual.update(
            plan_job_status=view.get("status"),
            plan_job_attempts=view.get("attempts"),
            plan_job_error_code=view.get("error_code"),
            plan_job_last_error_code=view.get("last_error_code"),
            plan_job_duration_ms=view.get("duration_ms"),
        )
        if perf_counter() >= deadline:
            actual.update(
                plan_job_poll_timed_out=True,
                detail="Planning job wait deadline exceeded; the job may still be running",
            )
            return None, None, actual
        if view.get("status") == "ready":
            return view.get("plan"), None, actual
        if view.get("status") == "failed":
            actual["detail"] = view.get("error")
            return None, view.get("error_code"), actual
        if view.get("status") != "planning":
            actual["detail"] = "Unexpected planning job status"
            return None, None, actual

        sleep(min(poll_interval, max(0, deadline - perf_counter())))
        method = "GET"


def latest_event(code, events):
    if code == 200 and isinstance(events, list) and events and isinstance(events[-1], dict):
        return events[-1]
    return {}


def event_ticket_ids(event):
    details = event.get("details", {})
    try:
        ticket_ids = details.get("ticket_ids")
        if ticket_ids is not None:
            return ticket_ids
        return [ticket["id"] for ticket in details.get("tickets", [])]
    except (AttributeError, KeyError, TypeError):
        return None


def task_records(items):
    if not isinstance(items, list):
        return None
    records = []
    for item in items:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("ticket_id"), str)
            or not isinstance(item.get("summary"), str)
            or not item["summary"].strip()
        ):
            return None
        records.append({"ticket_id": item["ticket_id"], "summary": item["summary"]})
    return sorted(records, key=lambda item: item["ticket_id"])


def complete_followup(base_url, path, plan, expected, waiting_event, result, token):
    actual = result["actual"]

    def fail(reason):
        actual["completion_error"] = reason
        return False

    digest = sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    writes = [step for step in plan["steps"] if step["tool"] == "create_followup_tasks"]
    details = waiting_event.get("details", {})
    actual["approval_plan_verified"] = (
        expected["tools"] == ["list_tickets", "summarize_tickets", "create_followup_tasks"]
        and len(writes) == 1
        and isinstance(details, dict)
        and details.get("plan_hash") == digest
        and details.get("step") == writes[0]["name"]
        and details.get("tool") == "create_followup_tasks"
        and type(details.get("task_count")) is int
        and details["task_count"] == len(expected["ticket_ids"])
    )
    if not actual["approval_plan_verified"]:
        return fail("approval_plan_mismatch")

    def tasks_are_empty(stage):
        code, tasks = request_json(base_url, path + "/tasks")
        actual[f"tasks_{stage}_http_status"] = code
        actual[f"tasks_{stage}_count"] = len(tasks) if isinstance(tasks, list) else None
        if code != 200 or tasks != []:
            result["tasks_verified"] = False
            return False
        return True

    if not tasks_are_empty("before_approval"):
        return fail("tasks_not_empty_before_approval")

    code, approval = request_json(
        base_url, path + "/approve", method="POST", payload={"plan_hash": digest},
        headers={"X-Axon-Approval-Token": token},
    )
    actual["approval_http_status"] = code
    result["approval_passed"] = (
        code == 200 and isinstance(approval, dict) and approval.get("status") == "RUNNING"
    )
    if not result["approval_passed"]:
        return fail("approval_failed")
    if not tasks_are_empty("before_resume"):
        return fail("tasks_not_empty_before_resume")

    code, resumed = request_json(base_url, path + "/run?plan=generated", method="POST")
    actual["resume_http_status"] = code
    actual["status"] = resumed.get("status") if isinstance(resumed, dict) else None
    if code != 200 or actual["status"] != "COMPLETED":
        return fail("resume_failed")

    workflow_code, workflow = request_json(base_url, path)
    events_code, events = request_json(base_url, path + "/events")
    tasks_code, tasks = request_json(base_url, path + "/tasks")
    completed = latest_event(events_code, events)
    completion_details = completed.get("details", {})
    recorded = (
        task_records(completion_details.get("followup_tasks"))
        if isinstance(completion_details, dict) else None
    )
    persisted = task_records(tasks)
    actual.update(
        workflow_http_status=workflow_code,
        persisted_status=workflow.get("status") if isinstance(workflow, dict) else None,
        completion_events_http_status=events_code,
        tasks_http_status=tasks_code,
        followup_tasks=persisted,
        ticket_ids=event_ticket_ids(completed),
    )
    result["tasks_verified"] = (
        tasks_code == 200 and persisted is not None and recorded is not None
        and persisted == recorded
        and [task["ticket_id"] for task in persisted] == sorted(expected["ticket_ids"])
    )
    matching_decisions = [
        event for event in events if isinstance(event, dict)
        and isinstance(event.get("details"), dict)
        and event.get("to_status") == "RUNNING"
        and event["details"].get("decision") == "approved"
        and event["details"].get("plan_hash") == digest
        and event["details"].get("step") == writes[0]["name"]
    ] if events_code == 200 and isinstance(events, list) else []
    actual["matching_approval_events"] = len(matching_decisions)
    verified = (
        workflow_code == 200 and actual["persisted_status"] == "COMPLETED"
        and completed.get("to_status") == "COMPLETED"
        and actual["ticket_ids"] == expected["ticket_ids"]
        and len(matching_decisions) == 1 and result["tasks_verified"]
    )
    return True if verified else fail("completion_verification_failed")


def evaluate_case(
    base_url, case, *, planning_mode="sync", planning_timeout=660, poll_interval=1,
    approve_followups=False, approval_token=None,
):
    if approve_followups and not approval_token:
        raise ValueError("AXON_APPROVAL_TOKEN is required to approve follow-ups")
    expected = case["expect"]
    expects_rejection = "rejection" in expected
    result = {
        "id": case["id"],
        "category": case["category"],
        "planner_passed": False,
        "tool_selection_passed": None if expects_rejection else False,
        "execution_passed": None,
        "passed": False,
        "actual": {},
    }
    if approve_followups:
        result.update(approval_wait_passed=None, approval_passed=None, tasks_verified=None)
    actual = result["actual"]
    code, workflow = request_json(
        base_url, "/workflows", method="POST", payload={"request": case["request"]}
    )
    if code != 201:
        actual.update(create_http_status=code, detail=workflow.get("detail"))
        return result

    path = f"/workflows/{workflow['id']}"
    result["workflow_id"] = workflow["id"]
    started = perf_counter()
    if planning_mode == "job":
        body, rejection, job_actual = plan_with_job(
            base_url, path, timeout=planning_timeout, poll_interval=poll_interval
        )
        actual.update(job_actual)
        has_plan = actual.get("plan_job_status") == "ready" and isinstance(body, dict)
    else:
        code, body = request_json(base_url, path + "/plan", method="POST")
        actual["plan_http_status"] = code
        rejection = body.get("detail") if code == 422 else None
        has_plan = code == 200
        if not has_plan:
            actual["detail"] = body.get("detail")
    result["plan_latency_ms"] = round((perf_counter() - started) * 1000, 1)
    if has_plan:
        try:
            if not expects_rejection:
                actual["tools"] = [step["tool"] for step in body["steps"]]
                result["tool_selection_passed"] = actual["tools"] == expected["tools"]
            actual["steps"] = canonical_steps(body)
        except (KeyError, TypeError):
            actual["detail"] = "Invalid plan response shape"
    if expects_rejection:
        result["planner_passed"] = rejection == expected["rejection"]
        result["passed"] = result["planner_passed"]
        return result

    result["planner_passed"] = has_plan and actual.get("steps") == expected_steps(expected)
    if not result["planner_passed"]:
        return result

    code, workflow = request_json(base_url, path + "/run?plan=generated", method="POST")
    events_code, events = request_json(base_url, path + "/events")
    last_event = latest_event(events_code, events)
    ticket_ids = event_ticket_ids(last_event)
    actual.update(run_http_status=code, status=workflow.get("status"), ticket_ids=ticket_ids)
    result["execution_passed"] = (
        code == 200
        and workflow.get("status") == expected["status"]
        and last_event.get("to_status") == expected["status"]
        and ticket_ids == expected["ticket_ids"]
    )
    if approve_followups and expected["status"] == "WAITING_FOR_APPROVAL":
        result["approval_wait_passed"] = result["execution_passed"]
        actual["initial_status"] = actual["status"]
        if result["execution_passed"]:
            result["execution_passed"] = complete_followup(
                base_url, path, body, expected, last_event, result, approval_token
            )
    elif approve_followups and expected["status"] != "COMPLETED":
        result["execution_passed"] = False
        actual["completion_error"] = "unsupported_expected_state"
    result["passed"] = result["execution_passed"]
    return result


def positive_seconds(value):
    seconds = float(value)
    if not isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("Must be a finite number greater than zero")
    return seconds


def redact_secret(value, secret):
    if not secret:
        return value
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]")
    if isinstance(value, dict):
        return {key: redact_secret(item, secret) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_secret(item, secret) for item in value]
    return value


def main():
    parser = argparse.ArgumentParser(description="Evaluate Axon through its HTTP API")
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--planning-mode", choices=("sync", "job"), default="sync",
        help="sync: one /plan call; job: poll the durable planning worker",
    )
    parser.add_argument(
        "--planning-timeout", type=positive_seconds, default=660,
        help="Maximum total wait per planning job in seconds (default: 660)",
    )
    parser.add_argument("--poll-interval", type=positive_seconds, default=1)
    parser.add_argument(
        "--approve-followups", action="store_true",
        help="Approve matching follow-up plans, resume, and verify stored tasks; requires AXON_APPROVAL_TOKEN",
    )
    parser.add_argument(
        "--fail-on-regression", action="store_true", help="Exit with status 1 if any case fails"
    )
    args = parser.parse_args()
    approval_token = getenv("AXON_APPROVAL_TOKEN") if args.approve_followups else None
    if args.approve_followups and not approval_token:
        parser.error("--approve-followups requires AXON_APPROVAL_TOKEN in the environment")
    base_url = args.base_url.rstrip("/")
    if request_json(base_url, "/health") != (200, {"status": "ok"}):
        raise SystemExit(f"Axon is unavailable at {base_url}")

    cases = [json.loads(line) for line in args.dataset.read_text().splitlines() if line.strip()]
    if not cases:
        raise SystemExit("The evaluation dataset is empty")

    results = []
    for index, case in enumerate(cases, 1):
        print(f"[{index}/{len(cases)}] {redact_secret(case['id'], approval_token)}", flush=True)
        result = evaluate_case(
            base_url, case, planning_mode=args.planning_mode,
            planning_timeout=args.planning_timeout, poll_interval=args.poll_interval,
            approve_followups=args.approve_followups, approval_token=approval_token,
        )
        results.append(result)
        if result["passed"]:
            print("  PASS")
        else:
            print("  FAIL", json.dumps(redact_secret(result["actual"], approval_token), ensure_ascii=False))

    latencies = sorted(row["plan_latency_ms"] for row in results if "plan_latency_ms" in row)
    execution_cases = [row for row in results if row["tool_selection_passed"] is not None]
    rejection_cases = [row for row in results if row["tool_selection_passed"] is None]
    execution_checks = sum(row["execution_passed"] is not None for row in execution_cases)
    execution_passed = sum(row["execution_passed"] is True for row in execution_cases)
    summary = {
        "planning_mode": args.planning_mode,
        "execution_scope": "completed_with_persisted_followups" if args.approve_followups else "dataset_expected_state",
        "cases": len(results),
        "passed": sum(row["passed"] for row in results),
        "planner_passed": sum(row["planner_passed"] for row in results),
        "planner_rejection_checks": len(rejection_cases),
        "planner_rejection_passed": sum(row["planner_passed"] for row in rejection_cases),
        "tool_selection_checks": len(execution_cases),
        "tool_selection_passed": sum(row["tool_selection_passed"] for row in execution_cases),
        "execution_eligible": len(execution_cases),
        "execution_checks": execution_checks,
        "execution_not_attempted": len(execution_cases) - execution_checks,
        "execution_passed": execution_passed,
        "execution_pass_rate_when_attempted": (
            round(execution_passed / execution_checks, 4) if execution_checks else None
        ),
        "execution_end_to_end_pass_rate": (
            round(execution_passed / len(execution_cases), 4) if execution_cases else None
        ),
        ("plan_job_latency_ms" if args.planning_mode == "job" else "plan_http_latency_ms"): {
            "samples": len(latencies),
            "p50": round(median(latencies), 1) if latencies else None,
            "p95": latencies[ceil(0.95 * len(latencies)) - 1] if latencies else None,
        },
    }
    if args.approve_followups:
        summary.update(
            approval_checks=sum(row.get("approval_passed") is not None for row in results),
            approval_passed=sum(row.get("approval_passed") is True for row in results),
            task_verification_checks=sum(row.get("tasks_verified") is not None for row in results),
            task_verification_passed=sum(row.get("tasks_verified") is True for row in results),
        )
    if args.planning_mode == "job":
        retried = [row for row in results if (row["actual"].get("plan_job_attempts") or 0) > 1]
        summary.update(
            planning_wait_timeout_seconds=args.planning_timeout,
            planning_jobs_retried=len(retried),
            planning_jobs_recovered=sum(
                row["actual"].get("plan_job_status") == "ready" for row in retried
            ),
        )
    print(json.dumps(redact_secret(summary, approval_token), indent=2))
    if args.output:
        report = {"summary": summary, "cases": results}
        args.output.write_text(json.dumps(redact_secret(report, approval_token), indent=2, ensure_ascii=False) + "\n")
    if args.fail_on_regression and summary["passed"] != summary["cases"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
