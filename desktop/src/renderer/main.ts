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

function escapeHtml(value: unknown): string {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

if (!window.flowpilot) {
  app.innerHTML = `
    <pre class="fatal-error">Electron preload did not expose window.flowpilot.

请确认你是通过 Electron 启动，而不是直接用浏览器打开 Vite 页面。
在 desktop 目录执行：

npm run build
npm run dev

如果仍然出现这个错误，请检查 desktop/dist/main/preload.cjs 是否存在。</pre>
  `;
  throw new Error("Missing window.flowpilot preload bridge");
}

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
let modelStatus = "";
let promptDraft = "";
let promptSelectionStart = 0;
let promptSelectionEnd = 0;
let promptIsComposing = false;
let promptWasFocused = false;
let uiError = "";

const demoPrompt = "打开记事本，输入 Hello FlowPilot";

function html(strings: TemplateStringsArray, ...values: unknown[]): string {
  return strings.reduce((result, part, index) => {
    const value = values[index];
    return result + part + (value === undefined ? "" : String(value));
  }, "");
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
  const tone = ["succeeded", "healthy", "ok", "active", "ready"].includes(normalized)
    ? "good"
    : ["failed", "cancelled", "unhealthy", "offline"].includes(normalized)
      ? "bad"
      : ["waiting_user", "paused", "queued", "preparing", "running"].includes(normalized)
        ? "warn"
        : "neutral";
  return `<span class="badge ${tone}">${escapeHtml(status)}</span>`;
}

function shortId(id: string): string {
  return id.length > 10 ? `${id.slice(0, 8)}...` : id;
}

function compactTime(value?: string): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function render(): void {
  capturePromptState();
  const ready = Boolean(runtimeState?.ready);
  app.innerHTML = html`
    <main class="codex-shell">
      <aside class="activity-bar">
        <div class="brand-mark">F</div>
        <button id="new-run" class="rail-button active" title="新任务">+</button>
        <button id="refresh-runs" class="rail-button" title="刷新任务">↻</button>
        <button id="refresh-skills" class="rail-button" title="刷新技能">◇</button>
      </aside>

      <aside class="history-pane">
        <header class="pane-header">
          <div>
            <h1>FlowPilot</h1>
            <p>Desktop agent</p>
          </div>
          ${badge(ready ? "ready" : "offline")}
        </header>

        <button id="new-run-wide" class="primary-action">New run</button>

        <section class="pane-section grow">
          <div class="section-title">
            <span>Runs</span>
            <span>${runs.length}</span>
          </div>
          <div class="run-list">
            ${runs.map((run) => html`
              <button class="run-item ${run.id === selectedRunId ? "selected" : ""}" data-run-id="${escapeHtml(run.id)}">
                <span class="run-title">${escapeHtml(run.user_input || run.id)}</span>
                <span class="run-meta">
                  ${badge(run.status)}
                  <span>${escapeHtml(shortId(run.id))}</span>
                </span>
              </button>
            `).join("") || `<div class="empty-state">No runs yet</div>`}
          </div>
        </section>

        <section class="pane-section skills-compact">
          <div class="section-title">
            <span>Skills</span>
            <span>${skills.length}</span>
          </div>
          <div class="skill-list">
            ${skills.slice(0, 8).map((skill) => html`
              <div class="skill-row">
                <strong>${escapeHtml(skill.name)}</strong>
                <span>${escapeHtml(skill.publishedVersion || skill.latestVersion)}</span>
              </div>
            `).join("") || `<div class="empty-state">No skills loaded</div>`}
          </div>
        </section>
      </aside>

      <section class="thread-pane">
        <header class="thread-header">
          <div class="thread-title">
            <span class="eyebrow">Agent run</span>
            <h2>${selectedRun ? escapeHtml(selectedRun.user_input || selectedRun.id) : "New desktop task"}</h2>
          </div>
          <div class="thread-actions">
            ${selectedRun ? badge(selectedRun.status) : ""}
            <button id="refresh-runtime" class="secondary-action">Runtime</button>
          </div>
        </header>

        <section class="thread-scroll">
          ${selectedRun ? renderRunThread(selectedRun) : renderEmptyThread()}
        </section>

        <footer class="composer">
          <textarea id="prompt" placeholder="${demoPrompt}">${escapeHtml(promptDraft)}</textarea>
          ${uiError ? `<div class="composer-error">${escapeHtml(uiError)}</div>` : ""}
          <div class="composer-bar">
            <span>${ready ? "Runtime is ready" : "Start runtime before running a task"}</span>
            <button id="create-run" ${ready ? "" : "disabled"}>Run</button>
          </div>
        </footer>
      </section>

      <aside class="inspector-pane">
        ${renderRuntimeCard(ready)}
        ${renderConfirmationCard()}
        ${renderModelCard()}
        ${renderEventsCard()}
        ${renderLogCard()}
      </aside>
    </main>
  `;
  bindEvents();
  restorePromptState();
}

function renderEmptyThread(): string {
  return html`
    <div class="empty-thread">
      <h3>Start with a desktop instruction</h3>
      <p>Describe what WinPeekaboo should do on the Windows desktop. The runtime, events, and confirmation prompts stay visible on the right.</p>
    </div>
  `;
}

function renderRunThread(run: RunState): string {
  const steps = run.steps || [];
  return html`
    <article class="message user-message">
      <div class="avatar">U</div>
      <div class="message-body">
        <div class="message-label">User</div>
        <p>${escapeHtml(run.user_input)}</p>
      </div>
    </article>

    <article class="message assistant-message">
      <div class="avatar">A</div>
      <div class="message-body">
        <div class="message-label">FlowPilot</div>
        <div class="step-stack">
          ${steps.map((step, index) => html`
            <div class="step-row">
              <span class="step-index">${index + 1}</span>
              <div>
                <strong>${escapeHtml(step.name)}</strong>
                <span>${escapeHtml((step.tool_names || []).join(", ") || step.result || step.error || "Waiting for result")}</span>
              </div>
              ${badge(step.status)}
            </div>
          `).join("") || `<div class="pending-line">Waiting for the agent to produce steps...</div>`}
        </div>
        ${run.output || run.error ? html`
          <pre class="run-output">${escapeHtml(run.output || run.error)}</pre>
        ` : ""}
      </div>
    </article>
  `;
}

function renderRuntimeCard(ready: boolean): string {
  return html`
    <section class="inspector-card">
      <div class="card-title">
        <h3>Runtime</h3>
        ${badge(ready ? "ready" : "offline")}
      </div>
      <dl class="facts">
        <dt>Source</dt><dd>${escapeHtml(runtimeState?.source ?? "-")}</dd>
        <dt>PID</dt><dd>${escapeHtml(runtimeState?.pid ?? "-")}</dd>
        <dt>URL</dt><dd>${escapeHtml(runtimeState?.baseUrl ?? "-")}</dd>
        <dt>Error</dt><dd>${escapeHtml(runtimeState?.lastError ?? "-")}</dd>
      </dl>
      <div class="button-row">
        <button id="start-runtime">Start</button>
        <button id="stop-runtime" class="secondary-action">Stop</button>
      </div>
    </section>
  `;
}

function renderConfirmationCard(): string {
  const pending = selectedRun?.pending_confirmation;
  if (!pending) {
    return html`
      <section class="inspector-card quiet">
        <div class="card-title">
          <h3>Confirmation</h3>
          ${badge("clear")}
        </div>
        <p>No pending approval.</p>
      </section>
    `;
  }

  return html`
    <section class="inspector-card attention">
      <div class="card-title">
        <h3>Confirmation</h3>
        ${badge("waiting_user")}
      </div>
      <pre class="data-block">${escapeHtml(JSON.stringify(pending, null, 2))}</pre>
      <div class="button-row">
        <button id="confirm-run">Approve</button>
        <button id="reject-run" class="danger-action">Reject</button>
      </div>
    </section>
  `;
}

function renderModelCard(): string {
  return html`
    <section class="inspector-card">
      <div class="card-title">
        <h3>Models</h3>
        <button id="model-health" class="secondary-action">Health</button>
      </div>
      <pre id="model-status" class="data-block compact">${escapeHtml(modelStatus || "Not checked")}</pre>
    </section>
  `;
}

function renderEventsCard(): string {
  const visibleEvents = events.slice(-12).reverse();
  return html`
    <section class="inspector-card grow-card">
      <div class="card-title">
        <h3>Events</h3>
        <span>${visibleEvents.length}</span>
      </div>
      <div class="event-list">
        ${visibleEvents.map((event) => html`
          <div class="event-row">
            <div>
              <strong>${escapeHtml(event.sequence)} ${escapeHtml(event.type)}</strong>
              <span>${escapeHtml(compactTime(event.timestamp))}</span>
            </div>
            <pre>${escapeHtml(JSON.stringify(event.data, null, 2))}</pre>
          </div>
        `).join("") || `<div class="empty-state">No events</div>`}
      </div>
    </section>
  `;
}

function renderLogCard(): string {
  return html`
    <section class="inspector-card log-card">
      <div class="card-title">
        <h3>Runtime log</h3>
        <span>${runtimeLogs.length}</span>
      </div>
      <pre class="runtime-log">${escapeHtml(runtimeLogs.slice(-80).join("\n"))}</pre>
    </section>
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
  document.querySelector("#new-run")?.addEventListener("click", resetComposer);
  document.querySelector("#new-run-wide")?.addEventListener("click", resetComposer);
  const prompt = document.querySelector<HTMLTextAreaElement>("#prompt");
  prompt?.addEventListener("input", () => {
    promptDraft = prompt.value;
    promptSelectionStart = prompt.selectionStart;
    promptSelectionEnd = prompt.selectionEnd;
    uiError = "";
  });
  prompt?.addEventListener("select", () => {
    promptSelectionStart = prompt.selectionStart;
    promptSelectionEnd = prompt.selectionEnd;
  });
  prompt?.addEventListener("compositionstart", () => {
    promptIsComposing = true;
  });
  prompt?.addEventListener("compositionend", () => {
    promptIsComposing = false;
    promptDraft = prompt.value;
    promptSelectionStart = prompt.selectionStart;
    promptSelectionEnd = prompt.selectionEnd;
  });

  document.querySelectorAll<HTMLButtonElement>(".run-item").forEach((button) => {
    button.addEventListener("click", () => {
      selectedRunId = button.dataset.runId || null;
      refreshSelectedRun();
    });
  });
}

function resetComposer(): void {
  selectedRunId = null;
  selectedRun = null;
  events = [];
  uiError = "";
  render();
}

function capturePromptState(): void {
  const prompt = document.querySelector<HTMLTextAreaElement>("#prompt");
  if (!prompt) return;
  promptWasFocused = document.activeElement === prompt;
  promptDraft = prompt.value;
  promptSelectionStart = prompt.selectionStart;
  promptSelectionEnd = prompt.selectionEnd;
}

function restorePromptState(): void {
  const prompt = document.querySelector<HTMLTextAreaElement>("#prompt");
  if (!prompt) return;
  prompt.value = promptDraft;
  if (document.hasFocus() && (promptWasFocused || promptIsComposing)) {
    prompt.focus();
    prompt.setSelectionRange(promptSelectionStart, promptSelectionEnd);
  }
  promptWasFocused = false;
}

async function refreshRuntime(): Promise<void> {
  runtimeState = await window.flowpilot.getRuntimeState();
  runtimeLogs = runtimeState.log || runtimeLogs;
  render();
}

async function startRuntime(): Promise<void> {
  runtimeState = await window.flowpilot.startRuntime();
  uiError = runtimeState.lastError || "";
  render();
  await refreshAll();
}

async function stopRuntime(): Promise<void> {
  runtimeState = await window.flowpilot.stopRuntime();
  render();
}

async function createRun(): Promise<void> {
  capturePromptState();
  const input = promptDraft.trim();
  if (!input) return;
  if (!runtimeState?.ready) {
    uiError = runtimeState?.lastError || "Runtime is not ready";
    render();
    return;
  }

  try {
    const run = await api<RunState>("/runs", "POST", { user_input: input });
    selectedRunId = run.id;
    selectedRun = run;
    promptDraft = "";
    promptSelectionStart = 0;
    promptSelectionEnd = 0;
    uiError = "";
    await refreshSelectedRun();
    await refreshRuns();
  } catch (error) {
    uiError = error instanceof Error ? error.message : String(error);
    await refreshRuntime().catch(() => undefined);
    render();
  }
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
  modelStatus = "checking...";
  render();
  try {
    const result = await api("/models/chat/health?probe=configuration", "POST");
    modelStatus = JSON.stringify(result, null, 2);
  } catch (error) {
    modelStatus = error instanceof Error ? error.message : String(error);
  }
  render();
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
