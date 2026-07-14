import "./styles.css";
import type { RuntimeState } from "./global";

type RunState = {
  id: string;
  status: string;
  user_input: string;
  output?: string;
  error?: string | null;
  pending_confirmation?: Record<string, unknown> | null;
  steps?: Array<{
    id: string;
    name: string;
    status: string;
    result?: string | null;
    error?: string | null;
    tool_names?: string[];
  }>;
};

type RunEvent = {
  id: string;
  run_id: string;
  sequence: number;
  type: string;
  data: Record<string, unknown>;
  timestamp: string;
};

type SkillSummary = {
  id: string;
  name: string;
  latestVersion: string;
  publishedVersion?: string | null;
  description?: string;
};

const app = document.querySelector<HTMLDivElement>("#app");
if (!app) throw new Error("Missing app root");

window.addEventListener("error", (event) => {
  app.innerHTML = `<pre class="fatal-error">${escapeHtml(event.message)}</pre>`;
});

window.addEventListener("unhandledrejection", (event) => {
  const reason = event.reason instanceof Error ? event.reason.message : String(event.reason);
  app.innerHTML = `<pre class="fatal-error">${escapeHtml(reason)}</pre>`;
});

let runtimeState: RuntimeState | null = null;
let selectedRunId: string | null = null;
let selectedRun: RunState | null = null;
let events: RunEvent[] = [];
let runs: RunState[] = [];
let skills: SkillSummary[] = [];
let runtimeLogs: string[] = [];

const demoPrompt = "打开记事本，输入 Hello FlowPilot";

function html(strings: TemplateStringsArray, ...values: unknown[]): string {
  return strings.reduce((result, part, index) => {
    const value = values[index];
    return result + part + (value === undefined ? "" : String(value));
  }, "");
}

function escapeHtml(value: unknown): string {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

async function api<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  const result = await window.flowpilot.request<T>({ path, method, body });
  if (!result.ok) {
    throw new Error(`${result.status}: ${JSON.stringify(result.data)}`);
  }
  return result.data;
}

function badge(status: string): string {
  const normalized = status.toLowerCase();
  const tone = ["succeeded", "healthy", "ok", "active"].includes(normalized)
    ? "good"
    : ["failed", "cancelled", "unhealthy"].includes(normalized)
      ? "bad"
      : ["waiting_user", "paused", "queued", "preparing"].includes(normalized)
        ? "warn"
        : "neutral";
  return `<span class="badge ${tone}">${escapeHtml(status)}</span>`;
}

function render(): void {
  const ready = runtimeState?.ready;
  app.innerHTML = html`
    <main class="shell">
      <aside class="sidebar">
        <div class="brand">
          <div>
            <h1>SEWC FlowPilot</h1>
            <p>Windows desktop automation runtime</p>
          </div>
          ${badge(ready ? "ready" : "offline")}
        </div>

        <section class="panel">
          <div class="panel-title">
            <h2>Runtime</h2>
            <button id="refresh-runtime" class="icon-button" title="刷新">↻</button>
          </div>
          <dl class="facts">
            <dt>Source</dt><dd>${escapeHtml(runtimeState?.source ?? "-")}</dd>
            <dt>PID</dt><dd>${escapeHtml(runtimeState?.pid ?? "-")}</dd>
            <dt>URL</dt><dd>${escapeHtml(runtimeState?.baseUrl ?? "-")}</dd>
            <dt>Error</dt><dd>${escapeHtml(runtimeState?.lastError ?? "-")}</dd>
          </dl>
          <div class="button-row">
            <button id="start-runtime">Start</button>
            <button id="stop-runtime" class="secondary">Stop</button>
          </div>
        </section>

        <section class="panel">
          <div class="panel-title">
            <h2>Models</h2>
            <button id="model-health" class="secondary">Health</button>
          </div>
          <pre id="model-status" class="compact-log"></pre>
        </section>

        <section class="panel grow">
          <div class="panel-title">
            <h2>Runtime Log</h2>
          </div>
          <pre class="runtime-log">${escapeHtml(runtimeLogs.slice(-80).join("\n"))}</pre>
        </section>
      </aside>

      <section class="workspace">
        <section class="run-composer">
          <textarea id="prompt" placeholder="${demoPrompt}"></textarea>
          <div class="composer-actions">
            <button id="create-run">Run Agent</button>
            <button id="refresh-runs" class="secondary">Refresh</button>
          </div>
        </section>

        <section class="content-grid">
          <section class="panel runs-panel">
            <div class="panel-title">
              <h2>Runs</h2>
            </div>
            <div class="run-list">
              ${runs.map((run) => html`
                <button class="run-item ${run.id === selectedRunId ? "selected" : ""}" data-run-id="${escapeHtml(run.id)}">
                  <span>${escapeHtml(run.user_input || run.id)}</span>
                  ${badge(run.status)}
                </button>
              `).join("") || `<div class="empty">No runs yet</div>`}
            </div>
          </section>

          <section class="panel detail-panel">
            <div class="panel-title">
              <h2>Run Detail</h2>
              ${selectedRun ? badge(selectedRun.status) : ""}
            </div>
            ${selectedRun ? runDetail(selectedRun) : `<div class="empty">Select a run</div>`}
          </section>

          <section class="panel skills-panel">
            <div class="panel-title">
              <h2>Skills</h2>
              <button id="refresh-skills" class="icon-button" title="刷新">↻</button>
            </div>
            <div class="skill-list">
              ${skills.map((skill) => html`
                <div class="skill-item">
                  <strong>${escapeHtml(skill.name)}</strong>
                  <span>${escapeHtml(skill.id)} @ ${escapeHtml(skill.publishedVersion || skill.latestVersion)}</span>
                </div>
              `).join("") || `<div class="empty">No skills loaded</div>`}
            </div>
          </section>

          <section class="panel events-panel">
            <div class="panel-title">
              <h2>Events</h2>
            </div>
            <div class="events">
              ${events.slice(-80).reverse().map((event) => html`
                <div class="event">
                  <span class="event-type">${escapeHtml(event.sequence)} ${escapeHtml(event.type)}</span>
                  <pre>${escapeHtml(JSON.stringify(event.data, null, 2))}</pre>
                </div>
              `).join("") || `<div class="empty">No events</div>`}
            </div>
          </section>
        </section>
      </section>
    </main>
  `;
  bindEvents();
}

function runDetail(run: RunState): string {
  const pending = run.pending_confirmation;
  return html`
    <div class="detail-block">
      <label>Run ID</label>
      <code>${escapeHtml(run.id)}</code>
    </div>
    ${pending ? html`
      <div class="confirmation">
        <strong>Confirmation required</strong>
        <pre>${escapeHtml(JSON.stringify(pending, null, 2))}</pre>
        <div class="button-row">
          <button id="confirm-run">Approve</button>
          <button id="reject-run" class="danger">Reject</button>
        </div>
      </div>
    ` : ""}
    <div class="steps">
      ${(run.steps || []).map((step) => html`
        <div class="step">
          <div>
            <strong>${escapeHtml(step.name)}</strong>
            <span>${escapeHtml((step.tool_names || []).join(", "))}</span>
          </div>
          ${badge(step.status)}
        </div>
      `).join("")}
    </div>
    <label>Output</label>
    <pre class="output">${escapeHtml(run.output || run.error || "")}</pre>
  `;
}

function bindEvents(): void {
  document.querySelector("#refresh-runtime")?.addEventListener("click", refreshRuntime);
  document.querySelector("#start-runtime")?.addEventListener("click", startRuntime);
  document.querySelector("#stop-runtime")?.addEventListener("click", stopRuntime);
  document.querySelector("#create-run")?.addEventListener("click", createRun);
  document.querySelector("#refresh-runs")?.addEventListener("click", refreshRuns);
  document.querySelector("#refresh-skills")?.addEventListener("click", refreshSkills);
  document.querySelector("#model-health")?.addEventListener("click", checkModelHealth);
  document.querySelector("#confirm-run")?.addEventListener("click", () => confirmRun(true));
  document.querySelector("#reject-run")?.addEventListener("click", () => confirmRun(false));

  document.querySelectorAll<HTMLButtonElement>(".run-item").forEach((button) => {
    button.addEventListener("click", () => {
      selectedRunId = button.dataset.runId || null;
      refreshSelectedRun();
    });
  });
}

async function refreshRuntime(): Promise<void> {
  runtimeState = await window.flowpilot.getRuntimeState();
  runtimeLogs = runtimeState.log || runtimeLogs;
  render();
}

async function startRuntime(): Promise<void> {
  runtimeState = await window.flowpilot.startRuntime();
  render();
  await refreshAll();
}

async function stopRuntime(): Promise<void> {
  runtimeState = await window.flowpilot.stopRuntime();
  render();
}

async function createRun(): Promise<void> {
  const input = document.querySelector<HTMLTextAreaElement>("#prompt")?.value.trim();
  if (!input) return;
  const run = await api<RunState>("/runs", "POST", { user_input: input });
  selectedRunId = run.id;
  await refreshSelectedRun();
  await refreshRuns();
}

async function refreshRuns(): Promise<void> {
  runs = await api<RunState[]>("/runs");
  if (!selectedRunId && runs.length) selectedRunId = runs[0].id;
  render();
}

async function refreshSelectedRun(): Promise<void> {
  if (!selectedRunId) return;
  selectedRun = await api<RunState>(`/runs/${selectedRunId}`);
  events = await api<RunEvent[]>(`/runs/${selectedRunId}/events`);
  render();
}

async function refreshSkills(): Promise<void> {
  skills = await api<SkillSummary[]>("/skills");
  render();
}

async function confirmRun(approved: boolean): Promise<void> {
  if (!selectedRunId) return;
  await api(`/runs/${selectedRunId}/confirm`, "POST", { approved });
  await refreshSelectedRun();
}

async function checkModelHealth(): Promise<void> {
  const target = document.querySelector<HTMLPreElement>("#model-status");
  if (target) target.textContent = "checking...";
  try {
    const result = await api("/models/chat/health?probe=configuration", "POST");
    if (target) target.textContent = JSON.stringify(result, null, 2);
  } catch (error) {
    if (target) target.textContent = error instanceof Error ? error.message : String(error);
  }
}

async function refreshAll(): Promise<void> {
  try {
    await refreshRuntime();
    await Promise.all([refreshRuns(), refreshSkills()]);
    if (selectedRunId) await refreshSelectedRun();
  } catch (error) {
    runtimeLogs.push(error instanceof Error ? error.message : String(error));
    render();
  }
}

window.flowpilot.onRuntimeLog((line) => {
  runtimeLogs.push(line);
  runtimeLogs = runtimeLogs.slice(-200);
  render();
});

window.flowpilot.onRuntimeState((state) => {
  runtimeState = state;
  render();
});

setInterval(() => {
  if (selectedRunId) {
    refreshSelectedRun().catch(() => undefined);
  }
}, 1500);

refreshAll();
