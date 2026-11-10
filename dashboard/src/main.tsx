import { useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

type Workflow = { id: string; request: string; status: string };
type Step = {
  name: string;
  tool: string;
  depends_on: string[];
  arguments: { limit?: number };
};
type Plan = { steps: Step[] };
type PlanJob = {
  status: "idle" | "planning" | "ready" | "failed";
  plan: Plan | null;
  duration_ms: number | null;
  error: string | null;
};
type WorkflowEvent = {
  id: number;
  from_status: string;
  to_status: string;
  created_at: string;
  details: Record<string, unknown>;
};

const exampleRequest =
  "Find the two highest-priority tickets, summarize each one, and create a follow-up task for each.";

async function api<T>(
  path: string, options?: RequestInit, onResponse?: (response: Response) => void,
): Promise<T> {
  const response = await fetch(path, options);
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    throw new Error(`Axon request failed or returned invalid JSON (HTTP ${response.status})`);
  }
  if (!response.ok) {
    const detail = body && typeof body === "object" && "detail" in body ? body.detail : undefined;
    throw new Error(typeof detail === "string" ? detail : `Axon request failed (HTTP ${response.status})`);
  }
  onResponse?.(response);
  return body as T;
}

function post<T>(path: string, body?: object, headers: Record<string, string> = {}) {
  return api<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...headers },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

function ticketIds(details: Record<string, unknown>): string[] {
  if (Array.isArray(details.ticket_ids)) {
    return details.ticket_ids.filter((id): id is string => typeof id === "string");
  }
  if (Array.isArray(details.tickets)) {
    return details.tickets.flatMap((ticket) => typeof ticket?.id === "string" ? [ticket.id] : []);
  }
  return [];
}

function statusText(status: string) {
  return status.replaceAll("_", " ");
}

function approvalTokenError(value: string) {
  try {
    new Headers({ "X-Axon-Approval-Token": value });
    return "";
  } catch {
    return "Paste the operator token exactly as configured, without extra formatting.";
  }
}

function App() {
  const [text, setText] = useState(exampleRequest);
  const [workflow, setWorkflow] = useState<Workflow | null>(null);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [events, setEvents] = useState<WorkflowEvent[]>([]);
  const [savedWorkflowId, setSavedWorkflowId] = useState("");
  const [token, setToken] = useState("");
  const [copiedId, setCopiedId] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [planningMs, setPlanningMs] = useState<number | null>(null);
  const [executionMs, setExecutionMs] = useState<number | null>(null);
  const creation = useRef<{ text: string; key: string } | null>(null);

  const path = workflow ? `/workflows/${workflow.id}` : "";
  const status = workflow?.status ?? "READY";
  const approval = status === "WAITING_FOR_APPROVAL"
    ? [...events].reverse().find((event) => event.to_status === "WAITING_FOR_APPROVAL")
    : undefined;
  const digest = approval?.details.plan_hash;
  const approvalTickets = approval ? ticketIds(approval.details) : [];
  const approvalToken = token.trim();
  const tokenError = approvalTokenError(approvalToken);
  const completed = events.find((event) => event.to_status === "COMPLETED");
  const tasks = completed?.details.followup_tasks;
  const resultTasks = Array.isArray(tasks) ? tasks.filter(
    (task): task is { ticket_id: string; summary: string } =>
      task !== null && typeof task === "object"
      && typeof task.ticket_id === "string" && typeof task.summary === "string",
  ) : [];
  const durationText = (ms: number | null) =>
    ms === null ? "—" : ms < 1000 ? `${ms.toFixed(1)} ms` : `${(ms / 1000).toFixed(2)} s`;
  const canRun = workflow && plan && ["PENDING", "RUNNING", "RETRYING"].includes(workflow.status);
  const overviewStatus = workflow?.status ?? "READY";
  const overviewNeedsApproval = overviewStatus === "WAITING_FOR_APPROVAL";
  const overviewStats = [
    {
      label: "Workflow status",
      value: overviewNeedsApproval
        ? "Waiting"
        : overviewStatus.charAt(0) + overviewStatus.slice(1).toLowerCase().replaceAll("_", " "),
      detail: overviewNeedsApproval ? "Operator approval required" : "Current workflow",
    },
    {
      label: "Validated steps",
      value: plan ? String(plan.steps.length) : "—",
      detail: plan ? "Steps in the current plan" : "Preview a plan to see its steps",
    },
    { label: "Planning", value: durationText(planningMs), detail: "Current plan generation" },
    { label: "Execution", value: durationText(executionMs), detail: "Run and Resume in this tab" },
  ];

  async function perform(label: string, action: () => Promise<void>) {
    setBusy(label);
    setError("");
    try {
      await action();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Request failed");
    } finally {
      setBusy("");
    }
  }

  async function create() {
    const value = text.trim();
    if (creation.current?.text !== value) {
      creation.current = { text: value, key: crypto.randomUUID() };
    }
    const next = await post<Workflow>("/workflows", { request: value }, {
      "Idempotency-Key": creation.current.key,
    });
    creation.current = null;
    setWorkflow(next);
    setPlan(null);
    setEvents([]);
    setToken("");
    setPlanningMs(null);
    setExecutionMs(null);
  }

  async function openSavedWorkflow() {
    const id = savedWorkflowId.trim();
    if (!/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(id)) {
      throw new Error("Enter a valid workflow ID.");
    }

    const savedPath = `/workflows/${id}`;
    const [next, history, planJob] = await Promise.all([
      api<Workflow>(savedPath, { cache: "no-store" }),
      api<WorkflowEvent[]>(savedPath + "/events", { cache: "no-store" }),
      api<PlanJob>(savedPath + "/plan-job", { cache: "no-store" }).catch(() => null),
    ]);

    setWorkflow(next);
    setText(next.request);
    setEvents(history);
    setToken("");
    setCopiedId("");
    setPlan(planJob?.status === "ready" ? planJob.plan : null);
    setPlanningMs(planJob?.status === "ready" ? planJob.duration_ms : null);
    if (workflow?.id !== next.id) setExecutionMs(null);
    if (planJob === null) setError("Workflow opened. Its saved plan could not be loaded.");
  }

  async function refresh() {
    const [next, history] = await Promise.all([
      api<Workflow>(path),
      api<WorkflowEvent[]>(path + "/events"),
    ]);
    setWorkflow(next);
    setEvents(history);
  }

  async function previewPlan() {
    const jobPath = path + "/plan-job";
    let job = await post<PlanJob>(jobPath, {});
    const deadline = Date.now() + 220_000;

    while (job.status === "planning" && Date.now() < deadline) {
      await new Promise<void>((resolve) => window.setTimeout(resolve, 2500));
      job = await api<PlanJob>(jobPath, { cache: "no-store" });
    }
    if (job.status !== "ready" || job.plan === null) {
      throw new Error(job.error ?? "Planning did not complete. Try Preview plan again.");
    }

    setPlan(job.plan);
    if (job.duration_ms !== null) setPlanningMs(job.duration_ms);
  }

  async function run() {
    const next = await api<Workflow>(path + "/run?plan=generated", { method: "POST" }, (response) => {
      const duration = response.headers.get("Server-Timing")?.match(/(?:^|,\s*)execution;dur=(\d+(?:\.\d+)?)(?:[;,]|$)/)?.[1];
      if (duration !== undefined) {
        setExecutionMs((previous) => (previous ?? 0) + Number(duration));
      }
    });
    setWorkflow(next);
    setEvents(await api<WorkflowEvent[]>(path + "/events"));
  }

  async function decide(decision: "approve" | "reject") {
    if (typeof digest !== "string" || !approvalToken || tokenError) return;
    setWorkflow(await post<Workflow>(path + "/" + decision, { plan_hash: digest }, {
      "X-Axon-Approval-Token": approvalToken,
    }));
    setToken("");
    setEvents(await api<WorkflowEvent[]>(path + "/events"));
  }

  async function copyWorkflowId() {
    if (!workflow) return;
    if (!navigator.clipboard) throw new Error("Clipboard unavailable. Select the workflow ID to copy it.");
    await navigator.clipboard.writeText(workflow.id);
    setCopiedId(workflow.id);
  }

  return (
    <div className="workspace-shell">
      <aside className="sidebar">
        <div className="brand-block">
          <span className="axon-brand">AXON</span>
          <span className="brand-subtitle">Operator Console</span>
        </div>

        <nav className="sidebar-nav" aria-label="Primary navigation">
          <button className="nav-item active" type="button">Overview</button>
          <button className="nav-item" type="button">Workflows</button>
          <button className="nav-item" type="button">Executions</button>
          <button className="nav-item" type="button">Approvals</button>
        </nav>

        <div className="sidebar-card">
          <p className="eyebrow">ENGINE STATUS</p>
          <div className="engine-pill">
            <span className="status-dot" aria-hidden="true" />
            Local workflow engine
          </div>
          <small>Deterministic orchestration and policy enforcement.</small>
        </div>
      </aside>

      <main className="workspace">
        <div className="topbar">
          <p className="eyebrow"><span className="axon-brand mini">AXON <span>OPERATOR CONSOLE</span></span></p>
          <span className="environment">Local workflow engine</span>
        </div>

        <header className="masthead">
          <div>
            <h1>Workflows</h1>
            <p className="intro">Turn a support request into a validated plan, review its impact, and control what runs.</p>
          </div>
        </header>

        <section className="stat-grid" aria-label="Workflow overview metrics">
          {overviewStats.map((stat) => (
            <div key={stat.label} className="stat-card">
              <span>{stat.label}</span>
              <strong>{stat.value}</strong>
              <small>{stat.detail}</small>
            </div>
          ))}
        </section>

        <div className="workflow-bar">
          <span className="badge" data-status={status}>{statusText(status)}</span>
          {workflow ? (
            <div className="workflow-id">
              <span>Workflow ID</span>
              <code>{workflow.id}</code>
              <button className="quiet" type="button" disabled={!!busy}
                onClick={() => void perform("Copying workflow ID…", copyWorkflowId)}>
                {copiedId === workflow.id ? "Copied" : "Copy ID"}
              </button>
            </div>
          ) : <span className="muted">Start with the example request below.</span>}
        </div>

        <div className="notice" role="status">{busy}</div>
        {error && <p className="error" role="alert">{error}</p>}

        <div className="columns">
          <div className="stack">
            <section className="panel" aria-labelledby="request-title">
              <p className="eyebrow">01 / REQUEST</p>
              <h2 id="request-title">Create a workflow</h2>
              <label htmlFor="request">Support request</label>
              <textarea id="request" maxLength={4000} value={text}
                onChange={(event) => setText(event.target.value)} />
              <button className="primary" disabled={!!busy || !text.trim()}
                onClick={() => void perform("Creating workflow…", create)}>Create workflow</button>
            <details className="saved-workflow">
              <summary>Open a saved workflow</summary>
              <form onSubmit={(event) => {
                event.preventDefault();
                if (!busy && savedWorkflowId.trim()) {
                  void perform("Opening workflow…", openSavedWorkflow);
                }
              }}>
                <label htmlFor="saved-workflow-id">Workflow ID</label>
                <div className="saved-workflow-controls">
                  <input id="saved-workflow-id" value={savedWorkflowId} maxLength={64}
                    placeholder="Paste a workflow ID" autoComplete="off" spellCheck={false}
                    disabled={!!busy} onChange={(event) => setSavedWorkflowId(event.target.value)} />
                  <button type="submit" disabled={!!busy || !savedWorkflowId.trim()}>Open workflow</button>
                </div>
                <small>Use the ID from a previous run to open its saved details.</small>
              </form>
            </details>
            </section>

            <section className="panel plan-panel" aria-labelledby="plan-title">
              <div className="panel-heading">
                <div><p className="eyebrow">02 / PLAN</p><h2 id="plan-title">Execution plan</h2></div>
                {plan && <span className="tag validated">Validated · {plan.steps.length} steps</span>}
              </div>
              {plan ? (
                <ol className="steps">
                  {plan.steps.map((step, index) => (
                    <li key={step.name}>
                      <span className="step-number" aria-hidden="true">{String(index + 1).padStart(2, "0")}</span>
                      <div>
                        <code>{step.tool}</code>
                        <small>{step.arguments.limit !== undefined && `Limit ${step.arguments.limit} · `}
                          {step.depends_on.length ? `After ${step.depends_on.join(", ")}` : "Starts the plan"}
                        </small>
                      </div>
                      {step.tool === "create_followup_tasks" && <span className="tag write-tag">Writes data</span>}
                    </li>
                  ))}
                </ol>
              ) : <p className="empty">Create a workflow, then preview its validated plan here.</p>}
              <div className="actions">
                <button disabled={!!busy || !workflow || (workflow.status !== "PENDING" && !plan)}
                  onClick={() => void perform("Generating plan…", previewPlan)}>Preview plan</button>
                <button className="primary" disabled={!!busy || !canRun}
                  onClick={() => void perform("Running workflow…", run)}>
                  {status === "RUNNING" || status === "RETRYING" ? "Resume workflow" : "Run workflow"}
                </button>
              </div>
            </section>
          </div>

          <div className="stack">
            {approval && typeof digest === "string" && (
              <section className="panel approval" aria-labelledby="approval-title">
                <p className="eyebrow">OPERATOR DECISION</p>
                <h2 id="approval-title">Approve follow-up tasks</h2>
                {approvalTickets.length > 0 ? (
                  <p className="approval-impact">
                    Create <strong>{approvalTickets.length} follow-up {approvalTickets.length === 1 ? "task" : "tasks"}</strong>
                    {" for "}<code>{approvalTickets.join(", ")}</code>.
                  </p>
                ) : <p className="approval-impact">Review the proposed follow-up tasks before execution resumes.</p>}
                <details className="fingerprint">
                  <summary>Plan fingerprint</summary>
                  <code>{digest}</code>
                </details>
                <label htmlFor="approval-token">Operator token</label>
                <input id="approval-token" type="password" autoComplete="off" value={token}
                  aria-invalid={Boolean(tokenError)} aria-describedby={tokenError ? "approval-token-error" : undefined}
                  onChange={(event) => setToken(event.target.value)} />
                {tokenError && <p className="field-error" id="approval-token-error" role="alert">{tokenError}</p>}
                <div className="actions">
                  <button className="primary" disabled={!!busy || !approvalToken || !!tokenError}
                    onClick={() => void perform("Saving approval…", () => decide("approve"))}>Approve</button>
                  <button className="danger" disabled={!!busy || !approvalToken || !!tokenError}
                    onClick={() => void perform("Saving rejection…", () => decide("reject"))}>Reject</button>
                </div>
              </section>
            )}

            <section className="panel" aria-labelledby="results-title">
              <p className="eyebrow">03 / OUTPUT</p>
              <h2 id="results-title">Workflow results</h2>
              <dl className="metrics">
                <div><dt>Planning</dt><dd>{durationText(planningMs)}</dd></div>
                <div><dt>Execution</dt><dd>{durationText(executionMs)}</dd></div>
              </dl>
              <small>Execution totals Run and Resume calls in this tab.</small>
              {completed ? (
                <>
                  <div className="result-heading">
                    <p>Selected tickets <code>{ticketIds(completed.details).join(", ") || "—"}</code></p>
                    <span className="tag validated">{resultTasks.length} follow-up {resultTasks.length === 1 ? "task" : "tasks"}</span>
                  </div>
                  {resultTasks.length > 0 && (
                    <ul className="result-list">
                      {resultTasks.map((task) => (
                        <li key={task.ticket_id}><code>{task.ticket_id}</code><small>{task.summary}</small></li>
                      ))}
                    </ul>
                  )}
                </>
              ) : <p className="muted">{status === "CANCELLED" ? "This workflow was cancelled." : "Results appear when the workflow completes."}</p>}
            </section>

            <section className="panel" aria-labelledby="history-title">
              <div className="panel-heading">
                <div><p className="eyebrow">04 / AUDIT TRAIL</p><h2 id="history-title">Event history</h2></div>
                <button className="quiet" disabled={!!busy || !workflow}
                  onClick={() => void perform("Refreshing history…", refresh)}>Refresh</button>
              </div>
              {events.length ? (
                <ol className="timeline">
                  {events.map((event) => {
                    const ids = ticketIds(event.details);
                    return (
                      <li key={event.id}>
                        <span className="badge" data-status={event.to_status}>{statusText(event.to_status)}</span>
                        <small>{statusText(event.from_status)} → {statusText(event.to_status)}</small>
                        <time dateTime={event.created_at}>{new Date(event.created_at).toLocaleString()}</time>
                        {ids.length > 0 && <small>Tickets {ids.join(", ")}</small>}
                      </li>
                    );
                  })}
                </ol>
              ) : <p className="muted">Run a workflow to see its state changes.</p>}
            </section>
          </div>
        </div>
      </main>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
