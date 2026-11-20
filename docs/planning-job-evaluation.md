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

Datasets and expected results are unchanged. Follow-up cases still stop at
`WAITING_FOR_APPROVAL`; these reports do not establish successful writes after
approval. Datasets used during planner tuning should be described as regression
sets rather than evidence of performance on unseen requests.
