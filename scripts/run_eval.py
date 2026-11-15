import argparse
import json
from math import ceil
from pathlib import Path
from statistics import median
from time import perf_counter
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_DATASET = Path(__file__).resolve().parents[1] / "evals" / "planner_cases.jsonl"


def request_json(base_url, path, *, method="GET", payload=None):
    request = Request(
        base_url + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        try:
            response = urlopen(request, timeout=200)
        except HTTPError as error:
            response = error
        with response:
            return response.getcode(), json.load(response)
    except (URLError, TimeoutError, json.JSONDecodeError) as error:
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


def evaluate_case(base_url, case):
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
    actual = result["actual"]
    code, workflow = request_json(
        base_url, "/workflows", method="POST", payload={"request": case["request"]}
    )
    if code != 201:
        actual.update(create_http_status=code, detail=workflow.get("detail"))
        return result

    path = f"/workflows/{workflow['id']}"
    started = perf_counter()
    code, body = request_json(base_url, path + "/plan", method="POST")
    if code:
        result["plan_latency_ms"] = round((perf_counter() - started) * 1000, 1)
    actual["plan_http_status"] = code
    if code == 200:
        try:
            if not expects_rejection:
                actual["tools"] = [step["tool"] for step in body["steps"]]
                result["tool_selection_passed"] = actual["tools"] == expected["tools"]
            actual["steps"] = canonical_steps(body)
        except (KeyError, TypeError):
            actual["detail"] = "Invalid plan response shape"
    else:
        actual["detail"] = body.get("detail")

    if expects_rejection:
        result["planner_passed"] = code == 422 and actual["detail"] == expected["rejection"]
        result["passed"] = result["planner_passed"]
        return result

    result["planner_passed"] = code == 200 and actual.get("steps") == expected_steps(expected)
    if not result["planner_passed"]:
        return result

    code, workflow = request_json(base_url, path + "/run?plan=generated", method="POST")
    events_code, events = request_json(base_url, path + "/events")
    last_event = events[-1] if events_code == 200 and events else {}
    details = last_event.get("details", {})
    ticket_ids = details.get("ticket_ids")
    if ticket_ids is None:
        ticket_ids = [ticket["id"] for ticket in details.get("tickets", [])]
    actual.update(run_http_status=code, status=workflow.get("status"), ticket_ids=ticket_ids)
    result["execution_passed"] = (
        code == 200
        and workflow.get("status") == expected["status"]
        and last_event.get("to_status") == expected["status"]
        and ticket_ids == expected["ticket_ids"]
    )
    result["passed"] = result["execution_passed"]
    return result


def main():
    parser = argparse.ArgumentParser(description="Evaluate Axon through its HTTP API")
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--fail-on-regression", action="store_true", help="Exit with status 1 if any case fails"
    )
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")
    if request_json(base_url, "/health") != (200, {"status": "ok"}):
        raise SystemExit(f"Axon is unavailable at {base_url}")

    cases = [json.loads(line) for line in args.dataset.read_text().splitlines() if line.strip()]
    if not cases:
        raise SystemExit("The evaluation dataset is empty")

    results = []
    for index, case in enumerate(cases, 1):
        print(f"[{index}/{len(cases)}] {case['id']}", flush=True)
        result = evaluate_case(base_url, case)
        results.append(result)
        if result["passed"]:
            print("  PASS")
        else:
            print("  FAIL", json.dumps(result["actual"], ensure_ascii=False))

    latencies = sorted(row["plan_latency_ms"] for row in results if "plan_latency_ms" in row)
    execution_cases = [row for row in results if row["tool_selection_passed"] is not None]
    rejection_cases = [row for row in results if row["tool_selection_passed"] is None]
    execution_checks = sum(row["execution_passed"] is not None for row in execution_cases)
    execution_passed = sum(row["execution_passed"] is True for row in execution_cases)
    summary = {
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
        "plan_http_latency_ms": {
            "samples": len(latencies),
            "p50": round(median(latencies), 1) if latencies else None,
            "p95": latencies[ceil(0.95 * len(latencies)) - 1] if latencies else None,
        },
    }
    print(json.dumps(summary, indent=2))
    if args.output:
        report = {"summary": summary, "cases": results}
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    if args.fail_on_regression and summary["passed"] != summary["cases"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
