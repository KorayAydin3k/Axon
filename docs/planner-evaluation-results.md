# Planner evaluation results — 2026-11-20

These are recorded results from the local support-ticket demo using `qwen3:4b`.
They describe particular runs on fixed test sets. All three datasets have been
used during planner development; files named `holdout` are retained as regression
inputs and no longer provide evidence of performance on unseen requests.

## Sources and versions

The [manifest](../evals/results/2026-11-20/manifest.json) identifies the datasets,
report hashes, source paths and available commit metadata. The four reports and
the two-case job dataset were copied byte for byte from the supplied run files.
Their embedded summaries were not recalculated or replaced.

| Dataset snapshot | Cases | SHA-256 |
| --- | --- | --- |
| [planner_cases.jsonl](../evals/planner_cases.jsonl) | 12 | `a41d5e4598639ab2a4d0f7d445abf43596a458b0841022b4b9250f085bd22850` |
| [planner_holdout.jsonl](../evals/planner_holdout.jsonl) | 6 | `6b7aa8fa7f281eb30e5b95e9ed88480fc21e91c3917b5db3e57a254b6f8c4b4a` |
| [planner_holdout_v2.jsonl](../evals/planner_holdout_v2.jsonl) | 24 | `f778c43fc0ddef33f2dbdaa0f7fb4562146bb33d91aa56baf2be72c5225a8c5d` |

The synchronous run recorded source commit
[`774850d`](https://github.com/KorayAydin3k/Axon/commit/774850d11e1c7c2bab506146a87e7636ed28050b)
and model tag `qwen3:4b`. The later job run used changes subsequently committed as
[`560b33b`](https://github.com/KorayAydin3k/Axon/commit/560b33b), but its report did
not record an exact source SHA. The run date comes from the operator's logs;
the report JSON does not contain timestamps. These reports do not capture model
digests or initial cache state, so these are not controlled latency
benchmarks or a model comparison.

## Synchronous results

The runner created a new workflow for each case and called `/plan` once. A case
with a wrong plan or failed planning request was not executed. Planning checks
also include expected `unsupported` and `needs_clarification` rejections.

| Metric | Expanded set, 24 | Regression set, 12 | Legacy set, 6 |
| --- | --- | --- | --- |
| Planner checks, including expected rejections | 23/24 | 12/12 | 6/6 |
| Exact plans on supported requests | 15/16 | 8/8 | 3/3 |
| Correct tool selection on supported requests | 15/16 | 8/8 | 3/3 |
| Correct expected rejections | 8/8 | 4/4 | 3/3 |
| Run-state checks passed / attempted | 15/15 | 8/8 | 3/3 |
| Run-state checks passed / all supported cases | 15/16 | 8/8 | 3/3 |
| Supported cases not executed | 1 | 0 | 0 |

Reports: [expanded set](../evals/results/2026-11-20/sync-planner_holdout_v2.json),
[regression set](../evals/results/2026-11-20/sync-planner_cases.json), and
[legacy set](../evals/results/2026-11-20/sync-planner_holdout.json).

The combined result was **41/42 cases**. All 15 expected rejections passed.
Of the 27 requests expected to execute, 26 reached their expected run state:
19 completed read/summary workflows and seven workflows waiting for approval.

The remaining case, `holdout-v2-read-upper-bound`, returned HTTP 502 after
**180,103.5 ms** and did not execute. Its JSON contains a generic error message;
the corresponding operator-supplied `planning.failed` log identified
`ollama_timeout`. Slow model loading is a possible contributor, not proof that
all 180 seconds were spent loading the model.

## Durable job check

The [separate job report](../evals/results/2026-11-20/job-smoke.json) covers
[two inputs](../evals/results/2026-11-20/job-cases.jsonl) selected from the expanded
set. It submits each workflow through `/plan-job` once and polls the same job.

| Case | Result | Worker claims | Total planning wait | Observed outcome |
| --- | --- | --- | --- | --- |
| `holdout-v2-read-upper-bound` | Pass | 1 | 54,522.9 ms | `COMPLETED`, four available tickets, requested limit 10 |
| `holdout-v2-reject-change-priority-tr` | Pass | 1 | 1,018.2 ms | Failed planning job with expected `unsupported` code |

This run passed **2/2**, with **zero jobs retried and zero jobs recovered after
retry**. It does not demonstrate live timeout recovery or full coverage of the
24-case set through the worker. It also does not turn the earlier synchronous
suite into a 42/42 result. Timeout recovery and attempt limits are exercised
separately by the automated integration tests.

The job's successful planning attempt took 53,825.87 ms; the total measured wait
was 54,522.9 ms including enqueueing, queueing and polling. A polling HTTP 200
can contain a failed job; expected rejections are checked against the job's
machine-readable error code. See [durable planning evaluation](planning-job-evaluation.md).

## Latency scope

| Synchronous dataset | Samples | p50 | p95 |
| --- | --- | --- | --- |
| Expanded set | 24 | 8,503.1 ms | 104,262.4 ms |
| Regression set | 12 | 7,899.5 ms | 9,259.6 ms |
| Legacy set | 6 | 7,839.8 ms | 9,688.3 ms |

These are client-side planning HTTP measurements across all cases, including
fast deterministic rejections and failed responses. They do not isolate model
inference time. The runner uses the median for p50 and nearest-rank p95. The
two-case job run is too small for a useful percentile comparison; its individual
waits are shown above instead.

All four reports predate the 15-minute Compose model-residency setting in
[`a6153f2`](https://github.com/KorayAydin3k/Axon/commit/a6153f2840c4ee81f2b4ee378ade9a29033cabe3).
They do not measure its effect. That setting reduces repeated idle reloads; it
does not preload the model or establish that initial-load timeouts are fixed.
See [model residency](ollama-model-residency.md).

## What execution success means here

In these archived runs, the runner checked tool order, arguments and dependencies
before calling `/run?plan=generated`. It then checked the expected workflow state
and selected ticket IDs in the event history. Tool selection alone is less
strict than exact plan correctness: the right tools can still have the wrong
ticket limit.

The archived follow-up cases expected `WAITING_FOR_APPROVAL`. Those runs did not
submit an operator token, approve the plan, resume task creation or verify the
final task rows. Their `execution_end_to_end_pass_rate` field therefore means success up to
the dataset's expected state, not completion of every approval-gated write.
Post-approval writes, replay protection and concurrent resume have separate
integration tests.

The runner now offers an explicit `--approve-followups` completion mode with
persisted-task checks. That mode has a different `execution_scope` and does not
change these historical results. See [completing approval-gated cases](planning-job-evaluation.md#completing-approval-gated-cases).

CI runs `python -m pytest -q tests`, including the database integration suite
and evaluator unit tests. Those tests use controlled planner responses; a green
CI run is not a fresh Ollama evaluation. Older JSON reports remain in the
[results archive](../evals/results/) as records of earlier runs.
