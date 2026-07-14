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

type ViewMode = "chat" | "task" | "scheduled";

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
let runs: RunState[] = [];
let runtimeLogs: string[] = [];
let activeView: ViewMode = "chat";
let promptDraft = "";
let promptSelectionStart = 0;
let promptSelectionEnd = 0;
let promptIsComposing = false;
let promptWasFocused = false;
let uiError = "";

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

function currentTitle(): string {
  if (selectedRun) return selectedRun.user_input || selectedRun.id;
  if (activeView === "task") return "新建任务";
  if (activeView === "scheduled") return "已安排";
  return "新对话";
}

function composerPlaceholder(): string {
  if (activeView === "task") {
    return "描述要创建的一次性桌面任务，例如：打开记事本并输入测试内容";
  }
  return "输入你想让 FlowPilot 执行的桌面操作";
}

function render(): void {
  capturePromptState();
  const ready = Boolean(runtimeState?.ready);
  app.innerHTML = html`
    <main class="app-shell">
      <aside class="sidebar">
        <header class="brand">
          <div class="brand-mark">F</div>
          <div>
            <h1>FlowPilot</h1>
            <p>Windows desktop agent</p>
          </div>
        </header>

        <button id="new-chat" class="new-chat-button ${activeView === "chat" && !selectedRun ? "active" : ""}">
          <span>+</span>
          <strong>新对话</strong>
        </button>

        <nav class="nav-list">
          <button id="new-task" class="nav-item ${activeView === "task" && !selectedRun ? "active" : ""}">
            <span>▣</span>
            <strong>新建任务</strong>
          </button>
          <button id="scheduled" class="nav-item ${activeView === "scheduled" ? "active" : ""}">
            <span>◷</span>
            <strong>已安排</strong>
          </button>
        </nav>

        <section class="history-section">
          <div class="section-title">
            <span>聊天记录</span>
            <button id="refresh-runs" class="icon-button" title="刷新聊天记录">↻</button>
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
            `).join("") || `<div class="empty-state">暂无聊天记录</div>`}
          </div>
        </section>

        <footer class="runtime-footer">
          <div>
            <span class="muted">Runtime</span>
            ${badge(ready ? "ready" : "offline")}
          </div>
          <button id="${ready ? "stop-runtime" : "start-runtime"}" class="small-button">${ready ? "Stop" : "Start"}</button>
        </footer>
      </aside>

      <section class="main-pane">
        <header class="page-header">
          <div>
            <h2>${escapeHtml(currentTitle())}</h2>
            <p>${renderSubtitle()}</p>
          </div>
          ${selectedRun ? badge(selectedRun.status) : ""}
        </header>

        <section class="page-scroll">
          ${renderPageBody()}
        </section>

        ${activeView === "scheduled" ? "" : renderComposer(ready)}
      </section>
    </main>
  `;
  bindEvents();
  restorePromptState();
}

function renderSubtitle(): string {
  if (activeView === "scheduled") return "管理后续计划执行的桌面任务";
  if (activeView === "task" && !selectedRun) return "创建一个立即执行的一次性桌面任务";
  return "像聊天一样描述你要完成的桌面操作";
}

function renderPageBody(): string {
  if (activeView === "scheduled") return renderScheduledPage();
  if (selectedRun) return renderRunThread(selectedRun);
  if (activeView === "task") return renderTaskPage();
  return renderChatPage();
}

function renderChatPage(): string {
  return html`
    <div class="center-panel">
      <h3>开始一个新对话</h3>
      <p>在底部输入你想让 FlowPilot 执行的 Windows 桌面操作。发送后，它会创建一条运行记录，并在这里展示执行进度和结果。</p>
      <div class="hint-grid">
        <button class="prompt-suggestion" data-prompt="观察当前桌面窗口，并告诉我当前打开了什么">观察当前桌面</button>
        <button class="prompt-suggestion" data-prompt="打开记事本，输入 Hello FlowPilot">打开记事本测试</button>
      </div>
    </div>
  `;
}

function renderTaskPage(): string {
  return html`
    <div class="center-panel">
      <h3>新建任务</h3>
      <p>这里先创建立即执行的一次性任务。后续可以在这个页面扩展任务模板、参数、确认策略和执行前检查。</p>
      <div class="task-layout">
        <div class="task-card">
          <strong>一次性桌面任务</strong>
          <span>适合当前要马上测试的 WinPeekaboo 操作。</span>
        </div>
        <div class="task-card muted-card">
          <strong>计划任务</strong>
          <span>后续会接入到“已安排”页面进行管理。</span>
        </div>
      </div>
    </div>
  `;
}

function renderScheduledPage(): string {
  return html`
    <div class="scheduled-page">
      <div class="scheduled-header">
        <div>
          <h3>已安排</h3>
          <p>用于后续管理 schedule job：查看任务、启停、编辑计划和检查执行历史。</p>
        </div>
        <button id="create-scheduled" class="secondary-button" disabled>新建安排</button>
      </div>
      <div class="empty-schedule">
        <strong>暂无已安排任务</strong>
        <span>当前版本先保留入口和页面结构，后续接入 schedule job API 后在这里展示列表。</span>
      </div>
    </div>
  `;
}

function renderRunThread(run: RunState): string {
  const steps = run.steps || [];
  return html`
    <article class="message user-message">
      <div class="avatar">你</div>
      <div class="message-body user-bubble">
        <p>${escapeHtml(run.user_input)}</p>
      </div>
    </article>

    <article class="message assistant-message">
      <div class="avatar">F</div>
      <div class="message-body assistant-body">
        ${steps.length ? html`
          <div class="step-stack">
            ${steps.map((step, index) => html`
              <div class="step-row">
                <span class="step-index">${index + 1}</span>
                <div>
                  <strong>${escapeHtml(step.name)}</strong>
                  <span>${escapeHtml((step.tool_names || []).join(", ") || step.result || step.error || "等待结果")}</span>
                </div>
                ${badge(step.status)}
              </div>
            `).join("")}
          </div>
        ` : `<p class="muted">正在执行...</p>`}

        ${run.pending_confirmation ? html`
          <div class="confirmation">
            <strong>需要确认</strong>
            <pre>${escapeHtml(JSON.stringify(run.pending_confirmation, null, 2))}</pre>
            <div class="button-row">
              <button id="confirm-run">Approve</button>
              <button id="reject-run" class="danger-button">Reject</button>
            </div>
          </div>
        ` : ""}

        ${run.output || run.error ? html`
          <pre class="run-output">${escapeHtml(run.output || run.error)}</pre>
        ` : ""}
      </div>
    </article>
  `;
}

function renderComposer(ready: boolean): string {
  return html`
    <footer class="composer">
      <div class="composer-box">
        <textarea id="prompt" placeholder="${escapeHtml(composerPlaceholder())}">${escapeHtml(promptDraft)}</textarea>
        ${uiError ? `<div class="composer-error">${escapeHtml(uiError)}</div>` : ""}
        <div class="composer-bar">
          <span>${ready ? "Enter 发送，Shift+Enter 换行" : "Runtime is offline"}</span>
          <button id="create-run" ${ready ? "" : "disabled"}>发送</button>
        </div>
      </div>
    </footer>
  `;
}

function bindEvents(): void {
  document.querySelector("#start-runtime")?.addEventListener("click", startRuntime);
  document.querySelector("#stop-runtime")?.addEventListener("click", stopRuntime);
  document.querySelector("#create-run")?.addEventListener("click", createRun);
  document.querySelector("#refresh-runs")?.addEventListener("click", refreshRuns);
  document.querySelector("#confirm-run")?.addEventListener("click", () => confirmRun(true));
  document.querySelector("#reject-run")?.addEventListener("click", () => confirmRun(false));
  document.querySelector("#new-chat")?.addEventListener("click", () => resetConversation("chat"));
  document.querySelector("#new-task")?.addEventListener("click", () => resetConversation("task"));
  document.querySelector("#scheduled")?.addEventListener("click", () => resetConversation("scheduled"));

  document.querySelectorAll<HTMLButtonElement>(".prompt-suggestion").forEach((button) => {
    button.addEventListener("click", () => {
      promptDraft = button.dataset.prompt || "";
      promptSelectionStart = promptDraft.length;
      promptSelectionEnd = promptDraft.length;
      render();
    });
  });

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
  prompt?.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !promptIsComposing) {
      event.preventDefault();
      createRun();
    }
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
      activeView = "chat";
      selectedRunId = button.dataset.runId || null;
      refreshSelectedRun();
    });
  });
}

function resetConversation(mode: ViewMode): void {
  activeView = mode;
  selectedRunId = null;
  selectedRun = null;
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
    activeView = "chat";
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
  render();
}

async function refreshSelectedRun(): Promise<void> {
  if (!selectedRunId) return;
  selectedRun = await api<RunState>(`/runs/${selectedRunId}`);
  render();
}

async function confirmRun(approved: boolean): Promise<void> {
  if (!selectedRunId) return;
  await api(`/runs/${selectedRunId}/confirm`, "POST", { approved });
  await refreshSelectedRun();
}

async function refreshAll(): Promise<void> {
  try {
    await refreshRuntime();
    await refreshRuns();
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
