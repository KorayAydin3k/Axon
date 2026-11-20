# Evaluating durable planning jobs

The evaluator keeps its default `sync` mode: one request to `/plan`. A timeout
remains a failed case. Use `--planning-mode job` to measure the dashboard's
durable `/plan-job` path, including the worker's existing bounded retries.
The API, database, Ollama and planning worker must be running.

```bash
python3 scripts/run_eval.py \
  --base-url http://127.0.0.1:8002 \
  --planning-mode job \
  --dataset evals/planner_holdout_v2.jsonl \
  --output /tmp/axon-job-evaluation.json \
  --fail-on-regression
```

Job mode submits each workflow once and polls that same job. Each case has a
660-second total wait limit by default, configurable with `--planning-timeout`.
Each enqueue or polling HTTP call is limited to 10 seconds or the remaining
wait budget, whichever is shorter. A worker or API error fails the case.
Reaching the wait deadline does not cancel or resubmit the job; it may continue
on the worker. The report includes the workflow ID for diagnosis.

The following fields distinguish first-attempt success from recovery:

| Field | Meaning |
| --- | --- |
| `plan_job_attempts` | Number of worker claims, including recovery after a crash |
| `plan_job_last_error_code` | Most recent recorded failure, retained after recovery |
| `plan_job_error_code` | Terminal failure code; null after successful recovery |
| `plan_job_duration_ms` | Duration of the successful planning attempt, when available |
| `plan_latency_ms` | Full measured enqueue and polling time, including queueing, failed attempts and retry waits |
| `planning_jobs_retried` | Cases with more than one recorded attempt |
| `planning_jobs_recovered` | Those cases that eventually reached `ready` |

Job latency percentiles appear under `plan_job_latency_ms`; sync mode retains
`plan_http_latency_ms`. Store reports for each mode separately. A job-mode pass
after retry does not erase a sync-mode timeout or establish first-attempt
reliability. This change does not alter Ollama's timeout or the worker's retry
limit, and it does not remove slow model loading.

An expected rejection passes only when the failed job's machine-readable
`error_code` matches the dataset. A timeout never counts as `unsupported`.
The actual polling response remains HTTP 200; the evaluator does not rewrite
it as HTTP 422. Legacy jobs may have null attempt/error metadata.

Datasets and expected results are unchanged. By default, follow-up cases stop
at `WAITING_FOR_APPROVAL`. Datasets used during planner tuning should be described
as regression sets rather than evidence of performance on unseen requests.

## Completing approval-gated cases

`--approve-followups` opts into approving reviewed follow-up plans, resuming
execution and verifying the stored tasks. It creates simulated follow-up task
rows in the local database. Without this flag, an available token does not
enable automatic approval. With the flag, `AXON_APPROVAL_TOKEN` must be present
in the evaluator's environment before any workflow is created.

The [three-case completion subset](../evals/followup_completion_cases.jsonl)
contains unchanged cases from the existing regression datasets. It covers two
tickets, three tickets and a request for six tickets when only four exist.
It is a regression check, not a new holdout set.

To use the token already configured on the local API without printing it:

```bash
(
  set +x
  set -euo pipefail
  mkdir -p evals/results
  report_dir=$(mktemp -d "$PWD/evals/results/followup-completion.XXXXXX")
  approval_token=$(docker compose exec -T api printenv AXON_APPROVAL_TOKEN)
  AXON_APPROVAL_TOKEN="$approval_token" python3 scripts/run_eval.py \
    --base-url http://127.0.0.1:8002 \
    --planning-mode job \
    --approve-followups \
    --dataset evals/followup_completion_cases.jsonl \
    --output "$report_dir/result.json" \
    --fail-on-regression
)
```

This mode first checks the original dataset's expected approval wait. Before
sending a decision it verifies the exact plan, selected tickets, write step,
task count and plan fingerprint. The token is sent only to `/approve` and
authenticated requests do not follow redirects. Credentials are not added to
the report; console and report values also redact a token echoed by the server.

The evaluator requires an empty persisted task list both before approval and
before resume. After resume, it checks `COMPLETED` in the response, a fresh
workflow read and the event history. A matching approval audit event must exist.
`GET /workflows/{id}/tasks` reads the actual `followup_tasks` rows, ordered by
ticket ID. Their IDs and count must match the dataset, and their nonempty
summaries must match the completion event. Missing, extra or inconsistent rows
fail the case. Summary quality beyond that consistency is not scored.

Reports label the scope explicitly:

| Field | Meaning |
| --- | --- |
| `execution_scope: dataset_expected_state` | Default checks, which may stop at an approval wait |
| `execution_scope: completed_with_persisted_followups` | Opt-in completion checks, including stored follow-up rows |
| `approval_wait_passed` | The initial run reached the original expected approval wait |
| `approval_passed` | The approval call returned the expected running state |
| `tasks_verified` | Task persistence checks passed; false can also indicate premature writes |
| `approval_checks` / `approval_passed` in the summary | Number of attempted approval calls and successful decisions |
| `task_verification_checks` / `task_verification_passed` | Attempted persistence checks and successes |

In completion mode, an approval, resume, audit or task-verification failure
makes `execution_passed` false and lowers the execution pass rate. Failed plans
remain in its eligible denominator. The old datasets and archived reports are
not relabeled. The evaluator does not retry approval or resume calls; a failed
request can leave the workflow waiting, running or already completed. Use its
reported workflow ID to inspect it. Created workflows and tasks remain in the
local database after the evaluation.
