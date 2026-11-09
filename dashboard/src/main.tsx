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
type WorkflowEvent = {
  id: number;
  from_status: string;
  to_status: string;
  created_at: string;
  details: Record<string, unknown>;
};

const exampleRequest =
  "Find the two highest-priority tickets, summarize each one, and create a follow-up task for each.";

async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, options);
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    const message = response.ok ? "Axon returned an invalid response" : "Axon request failed";
    throw new Error(`${message} (HTTP ${response.status})`);
  }
  if (!response.ok) {
    const detail = body && typeof body === "object" && "detail" in body ? body.detail : undefined;
    throw new Error(typeof detail === "string" ? detail : `Axon request failed (HTTP ${response.status})`);
  }
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

function App() {
  const [text, setText] = useState(exampleRequest);
  const [workflow, setWorkflow] = useState<Workflow | null>(null);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [events, setEvents] = useState<WorkflowEvent[]>([]);
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const creation = useRef<{ text: string; key: string } | null>(null);

  const path = workflow ? `/workflows/${workflow.id}` : "";
  const waiting = workflow?.status === "WAITING_FOR_APPROVAL";
  const approval = waiting ? events.find((event) => event.to_status === "WAITING_FOR_APPROVAL") : undefined;
  const digest = approval?.details.plan_hash;
  const canRun = workflow && plan && ["PENDING", "RUNNING", "RETRYING"].includes(workflow.status);

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
  }

  async function refresh() {
    const [next, history] = await Promise.all([
      api<Workflow>(path),
      api<WorkflowEvent[]>(path + "/events"),
    ]);
    setWorkflow(next);
    setEvents(history);
  }

  async function run() {
    setWorkflow(await post<Workflow>(path + "/run?plan=generated"));
    setEvents(await api<WorkflowEvent[]>(path + "/events"));
  }

  async function decide(decision: "approve" | "reject") {
    if (typeof digest !== "string") return;
    setWorkflow(await post<Workflow>(path + "/" + decision, { plan_hash: digest }, {
      "X-Axon-Approval-Token": token.trim(),
    }));
    setToken("");
    setEvents(await api<WorkflowEvent[]>(path + "/events"));
  }

  return (
    <main>
      <header className="masthead">
        <div><p className="eyebrow">AXON / OPERATOR CONSOLE</p><h1>Workflows</h1></div>
        <span className="badge">{workflow?.status.replaceAll("_", " ") ?? "READY"}</span>
      </header>
      <p className="intro">Create a request, review the execution plan, and decide which actions may proceed.</p>
      <div className="notice" aria-live="polite">{busy}</div>
      {error && <p className="error" role="alert">{error}</p>}

      <div className="columns">
        <div className="stack">
          <section className="panel">
            <p className="eyebrow">01 / REQUEST</p>
            <h2>Create a workflow</h2>
            <label htmlFor="request">Request</label>
            <textarea id="request" maxLength={4000} value={text}
              onChange={(event) => setText(event.target.value)} />
            <button className="primary" disabled={!!busy || !text.trim()}
              onClick={() => void perform("Creating workflow…", create)}>Create workflow</button>
            {workflow && <p className="identifier">Workflow ID <code>{workflow.id}</code></p>}
          </section>

          <section className="panel">
            <p className="eyebrow">02 / PLAN</p>
            <h2>Review the plan</h2>
            <div className="actions">
              <button disabled={!!busy || !workflow || (workflow.status !== "PENDING" && !plan)}
                onClick={() => void perform("Generating plan…", async () => {
                  setPlan(await post<Plan>(path + "/plan"));
                })}>Preview plan</button>
              <button className="primary" disabled={!!busy || !canRun}
                onClick={() => void perform("Running workflow…", run)}>
                {workflow?.status === "RUNNING" || workflow?.status === "RETRYING" ? "Resume workflow" : "Run workflow"}
              </button>
            </div>
            {plan ? (
              <ol className="steps">
                {plan.steps.map((step) => (
                  <li key={step.name}>
                    <code>{step.tool}</code>
                    <small>{step.arguments.limit !== undefined && `Limit ${step.arguments.limit} · `}
                      {step.depends_on.length ? `After ${step.depends_on.join(", ")}` : "Starts the plan"}
                    </small>
                  </li>
                ))}
              </ol>
            ) : <p className="muted">The validated plan will appear here.</p>}
          </section>
        </div>

        <div className="stack">
          {approval && typeof digest === "string" && (
            <section className="panel approval">
              <p className="eyebrow">OPERATOR DECISION</p>
              <h2>Approve follow-up tasks</h2>
              <p>Tickets <code>{ticketIds(approval.details).join(", ")}</code></p>
              <p className="identifier">Plan fingerprint <code>{digest}</code></p>
              <label htmlFor="approval-token">Operator token</label>
              <input id="approval-token" type="password" autoComplete="off" value={token}
                onChange={(event) => setToken(event.target.value)} />
              <div className="actions">
                <button className="primary" disabled={!!busy || !token.trim()}
                  onClick={() => void perform("Saving approval…", () => decide("approve"))}>Approve</button>
                <button className="danger" disabled={!!busy || !token.trim()}
                  onClick={() => void perform("Saving rejection…", () => decide("reject"))}>Reject</button>
              </div>
            </section>
          )}

          <section className="panel">
            <div className="panel-heading">
              <div><p className="eyebrow">03 / AUDIT TRAIL</p><h2>Event history</h2></div>
              <button disabled={!!busy || !workflow}
                onClick={() => void perform("Refreshing history…", refresh)}>Refresh</button>
            </div>
            {events.length ? (
              <ol className="timeline">
                {events.map((event) => {
                  const ids = ticketIds(event.details);
                  return (
                    <li key={event.id}>
                      <strong>{event.to_status.replaceAll("_", " ")}</strong>
                      <small>{event.from_status.replaceAll("_", " ")} → {event.to_status.replaceAll("_", " ")}</small>
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
  );
}

createRoot(document.getElementById("root")!).render(<App />);
