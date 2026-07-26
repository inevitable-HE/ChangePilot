import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Activity,
  AlertTriangle,
  BarChart3,
  Check,
  ChevronDown,
  CircleDot,
  Clock3,
  Download,
  FileCheck2,
  Filter,
  FlaskConical,
  GitBranch,
  History,
  ListChecks,
  LoaderCircle,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  X,
  XCircle,
} from "lucide-react";

const TERMINAL = new Set([
  "succeeded",
  "failed",
  "cancelled",
  "compensated",
  "compensation_failed",
  "manual_intervention",
]);

async function api(path, options) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `Request failed: ${response.status}`);
  return payload;
}

function toneFor(state = "") {
  if (state === "succeeded") return "success";
  if (state === "compensated") return "cyan";
  if (state.includes("fail") || state === "manual_intervention") return "danger";
  if (state === "waiting_approval" || state === "result_unknown") return "warning";
  if (state === "running" || state === "compensating") return "active";
  return "neutral";
}

function Status({ value }) {
  return <span className={`status status-${toneFor(value)}`}>{value.replaceAll("_", " ")}</span>;
}

function IconButton({ label, children, ...props }) {
  return (
    <button className="icon-button" title={label} aria-label={label} {...props}>
      {children}
    </button>
  );
}

function EmptyState({ onCreate }) {
  return (
    <section className="empty-state">
      <div className="empty-icon"><GitBranch size={24} /></div>
      <h2>No change runs yet</h2>
      <p>Submit an isolated order-service upgrade to review the plan and approval boundary.</p>
      <button className="button button-primary" onClick={onCreate}>
        <Plus size={16} /> New change
      </button>
    </section>
  );
}

function NewChangeDialog({ open, onClose, onCreated }) {
  const [scenario, setScenario] = useState("success");
  const [summary, setSummary] = useState("Upgrade the order service and database schema.");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  if (!open) return null;

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const result = await api("/api/requests", {
        method: "POST",
        body: JSON.stringify({
          service_id: "order-service",
          current_version: "v1",
          target_version: "v2",
          change_summary: summary,
          success_conditions: ["V2 health and order smoke checks pass"],
          constraints: ["Require approval before schema migration"],
          scenario,
        }),
      });
      if (!result.run_id) throw new Error(result.errors?.join("; ") || "Request needs clarification");
      onCreated(result.run_id);
    } catch (failure) {
      setError(failure.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="dialog-backdrop" role="presentation">
      <form className="dialog" onSubmit={submit}>
        <header className="dialog-header">
          <div>
            <span className="eyebrow">Structured request</span>
            <h2>New change</h2>
          </div>
          <IconButton label="Close" type="button" onClick={onClose}><X size={18} /></IconButton>
        </header>
        <label>
          Change summary
          <textarea value={summary} onChange={(event) => setSummary(event.target.value)} rows={3} />
        </label>
        <div className="fixed-fields">
          <label>Service<input value="order-service" readOnly /></label>
          <label>Version<input value="v1 → v2" readOnly /></label>
        </div>
        <fieldset>
          <legend>Execution scenario</legend>
          {[
            ["success", "Successful upgrade", "Migrate, deploy and validate V2."],
            ["compensation", "Health failure", "Compensate service and schema to V1."],
            ["recovery", "Restart recovery", "Recover a committed migration result."],
          ].map(([value, title, detail]) => (
            <label className="scenario-option" key={value}>
              <input type="radio" name="scenario" value={value} checked={scenario === value} onChange={() => setScenario(value)} />
              <span><strong>{title}</strong><small>{detail}</small></span>
            </label>
          ))}
        </fieldset>
        {error && <div className="inline-error"><AlertTriangle size={16} />{error}</div>}
        <footer className="dialog-actions">
          <button type="button" className="button" onClick={onClose}>Cancel</button>
          <button className="button button-primary" disabled={busy || !summary.trim()}>
            {busy ? <LoaderCircle className="spin" size={16} /> : <Play size={16} />}
            Create plan
          </button>
        </footer>
      </form>
    </div>
  );
}

function RunList({ runs, selected, filter, setFilter, onSelect, onCreate, loading }) {
  const filtered = runs.filter((run) => filter === "all" || run.state === filter);
  return (
    <aside className="run-panel">
      <div className="panel-heading">
        <div><span className="eyebrow">Workspace</span><h2>Change runs</h2></div>
        <IconButton label="New change" onClick={onCreate}><Plus size={18} /></IconButton>
      </div>
      <div className="filter-row">
        <Filter size={14} />
        <select value={filter} onChange={(event) => setFilter(event.target.value)}>
          <option value="all">All states</option>
          <option value="waiting_approval">Waiting approval</option>
          <option value="succeeded">Succeeded</option>
          <option value="compensated">Compensated</option>
          <option value="manual_intervention">Manual intervention</option>
        </select>
      </div>
      <div className="run-list">
        {loading && <div className="panel-loading"><LoaderCircle className="spin" size={18} /> Loading runs</div>}
        {!loading && filtered.length === 0 && <p className="quiet">No runs match this filter.</p>}
        {filtered.map((run) => (
          <button className={`run-item ${selected === run.run_id ? "selected" : ""}`} key={run.run_id} onClick={() => onSelect(run.run_id)}>
            <div className="run-item-top"><strong>{run.definition_id}</strong><Status value={run.state} /></div>
            <span className="mono">{run.run_id.slice(0, 8)}</span>
            <div className="run-progress"><span style={{ width: `${Math.min(100, (run.last_event_sequence / 44) * 100)}%` }} /></div>
          </button>
        ))}
      </div>
    </aside>
  );
}

function PlanWorkspace({ snapshot, plan, activeStep, setActiveStep }) {
  const stateById = new Map(snapshot?.steps?.map((step) => [step.step_id, step.state]));
  return (
    <main className="plan-workspace">
      <header className="workspace-heading">
        <div>
          <span className="eyebrow">Execution plan</span>
          <h1>{snapshot?.summary.definition_id || "Select a run"}</h1>
        </div>
        {snapshot && <Status value={snapshot.summary.state} />}
      </header>
      <div className="plan-meta">
        <span><GitBranch size={15} /> v{plan?.definition_version ?? "—"}</span>
        <span><ShieldCheck size={15} /> policy guarded</span>
        <span><History size={15} /> {snapshot?.summary.last_event_sequence ?? 0} events</span>
      </div>
      <section className="step-list" aria-label="Plan steps">
        {plan?.steps.map((step, index) => {
          const state = stateById.get(step.step_id) || "pending";
          return (
            <button className={`step-row ${activeStep === step.step_id ? "active" : ""}`} key={step.step_id} onClick={() => setActiveStep(step.step_id)}>
              <span className={`step-index step-${toneFor(state)}`}>
                {state === "succeeded" ? <Check size={15} /> : index + 1}
              </span>
              <span className="step-copy">
                <span className="step-title"><strong>{step.step_id}</strong><Status value={state} /></span>
                <span>{step.tool.name}</span>
              </span>
              <span className="step-deps">{step.depends_on.length ? `after ${step.depends_on.join(", ")}` : "entry"}</span>
              {step.approval_required && <span className="risk-flag"><AlertTriangle size={14} /> high risk</span>}
              <ChevronDown size={16} />
            </button>
          );
        })}
      </section>
      {activeStep && plan && <StepDetail step={plan.steps.find((item) => item.step_id === activeStep)} />}
    </main>
  );
}

function StepDetail({ step }) {
  if (!step) return null;
  return (
    <section className="step-detail">
      <div className="detail-column">
        <span className="eyebrow">Intent</span>
        <p>{step.rationale}</p>
        <span className="eyebrow">Validation</span>
        <p>{step.validation_intent}</p>
      </div>
      <div className="detail-column">
        <span className="eyebrow">Evidence</span>
        {step.evidence_refs.length ? step.evidence_refs.map((ref) => <code key={ref}>{ref}</code>) : <p className="quiet">No external evidence required.</p>}
        {step.compensation_tool && <><span className="eyebrow">Compensation</span><p><RotateCcw size={14} /> {step.compensation_tool.name}</p></>}
      </div>
    </section>
  );
}

function ApprovalPanel({ snapshot, onDecision, onRecover, busy }) {
  const approval = snapshot?.pending_approval;
  const recoverable = snapshot?.steps.some((step) => step.state === "result_unknown");
  if (!approval && !recoverable) return null;
  return (
    <section className="approval-panel">
      <div className="approval-title"><ShieldCheck size={18} /><div><span className="eyebrow">{recoverable ? "Recovery required" : "Approval gate"}</span><h3>{recoverable ? "Reconcile unknown result" : approval.step_id}</h3></div></div>
      {approval && <>
        <p>{approval.risk_reasons.join(" · ") || "High-risk schema change requires operator approval."}</p>
        <dl><dt>Tool</dt><dd>{approval.tool_name}@{approval.tool_version}</dd><dt>Binding</dt><dd className="mono">{approval.binding_digest.slice(0, 12)}…</dd></dl>
        <div className="approval-actions">
          <button className="button button-danger" disabled={busy} onClick={() => onDecision("rejected")}><XCircle size={16} /> Reject</button>
          <button className="button button-primary" disabled={busy} onClick={() => onDecision("approved")}><Check size={16} /> Approve</button>
        </div>
      </>}
      {recoverable && <button className="button button-primary full" disabled={busy} onClick={onRecover}><RefreshCw size={16} /> Recover run</button>}
    </section>
  );
}

function AuditPanel({ snapshot, events, selectedEvent, setSelectedEvent, onDecision, onRecover, busy }) {
  return (
    <aside className="audit-panel">
      <ApprovalPanel snapshot={snapshot} onDecision={onDecision} onRecover={onRecover} busy={busy} />
      <div className="panel-heading audit-heading">
        <div><span className="eyebrow">Authoritative log</span><h2>Audit timeline</h2></div>
        {snapshot && <a className="icon-button" title="Download report" aria-label="Download report" href={`/api/runs/${snapshot.summary.run_id}/report.md`} target="_blank"><Download size={17} /></a>}
      </div>
      <div className="audit-list">
        {events.length === 0 && <p className="quiet">No events recorded.</p>}
        {[...events].reverse().map((event) => (
          <button className={`audit-event ${selectedEvent?.sequence === event.sequence ? "selected" : ""}`} key={event.sequence} onClick={() => setSelectedEvent(event)}>
            <span className="event-line" />
            <span className="event-dot"><CircleDot size={13} /></span>
            <span className="event-content"><strong>{event.event_type}</strong><small>#{event.sequence} · {event.step_id || "workflow"}</small></span>
          </button>
        ))}
      </div>
      {selectedEvent && <div className="event-inspector"><span className="eyebrow">Event #{selectedEvent.sequence}</span><pre>{JSON.stringify(selectedEvent.payload, null, 2)}</pre></div>}
    </aside>
  );
}

function EvaluationView({ items }) {
  const [selectedId, setSelectedId] = useState(null);
  const selected = items.find((item) => item.run_id === selectedId) || items[0];
  const percentageMetrics = Object.entries(selected?.aggregate_metrics || {})
    .filter(([name]) => name.endsWith("_rate"));
  const operationalMetrics = Object.entries(selected?.aggregate_metrics || {})
    .filter(([name]) => !name.endsWith("_rate"));
  const failedCases = selected?.case_results.filter((item) => !item.passed) || [];

  return (
    <section className="evaluation-view">
      <header className="evaluation-heading">
        <div><span className="eyebrow">Quality evidence</span><h1>Evaluation runs</h1></div>
        <div className="evaluation-mode"><FlaskConical size={15} /> Offline by default</div>
      </header>
      {items.length === 0 ? (
        <div className="empty-state compact">
          <FileCheck2 size={28} />
          <h2>No persisted evaluations</h2>
          <p>Run the offline core suite to establish the first comparable baseline.</p>
        </div>
      ) : (
        <div className="evaluation-layout">
          <aside className="evaluation-history">
            <span className="eyebrow">History</span>
            {items.map((item) => (
              <button
                className={`evaluation-run ${selected?.run_id === item.run_id ? "selected" : ""}`}
                key={item.run_id}
                onClick={() => setSelectedId(item.run_id)}
              >
                <span><strong>{item.metadata.dataset_id}</strong><Status value={item.passed ? "succeeded" : "failed"} /></span>
                <small>v{item.metadata.dataset_version} · {item.metadata.code_version}</small>
                <code>{item.run_id.slice(0, 8)}</code>
              </button>
            ))}
          </aside>
          <main className="evaluation-result">
            <div className="evaluation-summary">
              <div>
                <span className="eyebrow">Dataset</span>
                <h2>{selected.metadata.dataset_id}@{selected.metadata.dataset_version}</h2>
                <p>{selected.metadata.model} · metrics {selected.metadata.metrics_version}</p>
              </div>
              <div className="quality-score">
                <BarChart3 size={19} />
                <strong>{percentageMetrics.filter(([, value]) => value >= 1).length}/{percentageMetrics.length}</strong>
                <span>safety targets</span>
              </div>
            </div>
            <section className="metric-grid" aria-label="Evaluation metrics">
              {percentageMetrics.map(([name, value]) => (
                <div className="metric-item" key={name}>
                  <span>{name.replaceAll("_", " ")}</span>
                  <strong>{Math.round(value * 100)}%</strong>
                  <div><i style={{ width: `${Math.min(100, value * 100)}%` }} /></div>
                </div>
              ))}
            </section>
            <section className="evaluation-section">
              <div className="section-title">
                <div><span className="eyebrow">Regression set</span><h2>Case results</h2></div>
                <span>{selected.case_results.length - failedCases.length} passed · {failedCases.length} failed</span>
              </div>
              <div className="case-table">
                {selected.case_results.map((item) => (
                  <div className="case-row" key={item.case_id}>
                    <Status value={item.passed ? "succeeded" : "failed"} />
                    <strong>{item.case_id}</strong>
                    <span>{item.split}</span>
                    <span>{item.latency_ms} ms</span>
                    <span>{item.errors.join("; ") || "No errors"}</span>
                  </div>
                ))}
              </div>
            </section>
            <section className="evaluation-footer">
              <div>
                <span className="eyebrow">Budget telemetry</span>
                {operationalMetrics.map(([name, value]) => (
                  <p key={name}><span>{name.replaceAll("_", " ")}</span><strong>{value}</strong></p>
                ))}
              </div>
              <div>
                <span className="eyebrow">Baseline delta</span>
                {Object.keys(selected.baseline_delta).length === 0 ? <p className="quiet">Initial baseline. No prior run comparison.</p> :
                  Object.entries(selected.baseline_delta).map(([name, value]) => (
                    <p key={name}><span>{name.replaceAll("_", " ")}</span><strong className={value < 0 ? "negative" : ""}>{value > 0 ? "+" : ""}{value}</strong></p>
                  ))}
              </div>
              <div>
                <span className="eyebrow">Regressions</span>
                {selected.regressions.length === 0 ? <p className="evaluation-pass"><Check size={15} /> No threshold regressions</p> :
                  selected.regressions.map((name) => <p className="negative" key={name}>{name.replaceAll("_", " ")}</p>)}
              </div>
            </section>
          </main>
        </div>
      )}
    </section>
  );
}

export default function App() {
  const [runs, setRuns] = useState([]);
  const [selected, setSelected] = useState(null);
  const [snapshot, setSnapshot] = useState(null);
  const [plan, setPlan] = useState(null);
  const [events, setEvents] = useState([]);
  const [activeStep, setActiveStep] = useState(null);
  const [selectedEvent, setSelectedEvent] = useState(null);
  const [filter, setFilter] = useState("all");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [dialog, setDialog] = useState(false);
  const [view, setView] = useState("runs");
  const [evaluations, setEvaluations] = useState([]);

  const loadRuns = useCallback(async () => {
    try {
      const data = await api("/api/runs");
      setRuns(data);
      setSelected((current) => current || data[0]?.run_id || null);
    } catch (failure) { setError(failure.message); }
    finally { setLoading(false); }
  }, []);

  const loadSelected = useCallback(async (runId) => {
    if (!runId) return;
    try {
      const [nextSnapshot, nextPlan, nextEvents] = await Promise.all([
        api(`/api/runs/${runId}`),
        api(`/api/runs/${runId}/plan`),
        api(`/api/runs/${runId}/events`),
      ]);
      setSnapshot(nextSnapshot);
      setPlan(nextPlan);
      setEvents(nextEvents.events);
      setActiveStep((current) => current || nextPlan.steps[0]?.step_id);
      setError("");
    } catch (failure) { setError(failure.message); }
  }, []);

  useEffect(() => { loadRuns(); api("/api/evaluations").then(setEvaluations).catch(() => {}); }, [loadRuns]);
  useEffect(() => { loadSelected(selected); }, [selected, loadSelected]);
  useEffect(() => {
    if (!selected || snapshot && TERMINAL.has(snapshot.summary.state)) return undefined;
    const cursor = events.at(-1)?.sequence || 0;
    const source = new EventSource(`/api/runs/${selected}/events/stream?after=${cursor}`);
    source.addEventListener("audit", () => { loadSelected(selected); loadRuns(); });
    source.onerror = () => source.close();
    const fallback = window.setInterval(() => loadSelected(selected), 4000);
    return () => { source.close(); window.clearInterval(fallback); };
  }, [selected, snapshot?.summary.state, events.length, loadSelected, loadRuns]);

  async function decide(decision) {
    setBusy(true);
    try {
      const approval = snapshot.pending_approval;
      await api(`/api/runs/${selected}/approval`, {
        method: "POST",
        body: JSON.stringify({
          decision,
          expected_version: approval.version,
          binding_digest: approval.binding_digest,
          actor: "console-operator",
          reason: decision === "approved" ? "Reviewed evidence and compensation policy." : "Rejected from operations console.",
        }),
      });
      await Promise.all([loadSelected(selected), loadRuns()]);
    } catch (failure) { setError(`${failure.message}. Refreshing authoritative state.`); await loadSelected(selected); }
    finally { setBusy(false); }
  }

  async function recover() {
    setBusy(true);
    try {
      await api(`/api/runs/${selected}/recover`, { method: "POST", body: JSON.stringify({ expected_run_revision: snapshot.summary.revision }) });
      await Promise.all([loadSelected(selected), loadRuns()]);
    } catch (failure) { setError(failure.message); }
    finally { setBusy(false); }
  }

  const totals = useMemo(() => ({
    active: runs.filter((run) => !TERMINAL.has(run.state)).length,
    approvals: runs.filter((run) => run.pending_approval).length,
  }), [runs]);

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand"><span className="brand-mark">CP</span><div><strong>ChangePilot</strong><small>Operations console</small></div></div>
        <nav className="view-tabs">
          <button className={view === "runs" ? "active" : ""} onClick={() => setView("runs")}><Activity size={16} /> Runs</button>
          <button className={view === "evaluations" ? "active" : ""} onClick={() => setView("evaluations")}><ListChecks size={16} /> Evaluations</button>
        </nav>
        <div className="top-stats"><span><Clock3 size={14} /> {totals.active} active</span><span><ShieldCheck size={14} /> {totals.approvals} approvals</span><span className="connection"><span /> local</span></div>
      </header>
      {error && <div className="error-banner"><AlertTriangle size={16} /><span>{error}</span><button onClick={() => setError("")}><X size={16} /></button></div>}
      {view === "evaluations" ? <EvaluationView items={evaluations} /> : (
        runs.length === 0 && !loading ? <EmptyState onCreate={() => setDialog(true)} /> :
        <div className="operations-grid">
          <RunList runs={runs} selected={selected} filter={filter} setFilter={setFilter} onSelect={setSelected} onCreate={() => setDialog(true)} loading={loading} />
          <PlanWorkspace snapshot={snapshot} plan={plan} activeStep={activeStep} setActiveStep={setActiveStep} />
          <AuditPanel snapshot={snapshot} events={events} selectedEvent={selectedEvent} setSelectedEvent={setSelectedEvent} onDecision={decide} onRecover={recover} busy={busy} />
        </div>
      )}
      <NewChangeDialog open={dialog} onClose={() => setDialog(false)} onCreated={(runId) => { setDialog(false); setSelected(runId); loadRuns(); }} />
    </div>
  );
}
