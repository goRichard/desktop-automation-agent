import { app, BrowserWindow, ipcMain } from "electron";
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";

type RuntimeState = {
  baseUrl: string;
  token: string;
  pid: number | null;
  ready: boolean;
  source: "dev" | "packaged";
  lastError: string | null;
  log: string[];
};

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const isDev = Boolean(process.env.VITE_DEV_SERVER_URL);
const runtimePort = Number(process.env.FLOWPILOT_RUNTIME_PORT || "8765");
const runtimeToken = crypto.randomBytes(32).toString("base64url");
let runtimeProcess: ChildProcessWithoutNullStreams | null = null;
let mainWindow: BrowserWindow | null = null;

const runtimeState: RuntimeState = {
  baseUrl: `http://127.0.0.1:${runtimePort}`,
  token: runtimeToken,
  pid: null,
  ready: false,
  source: isDev ? "dev" : "packaged",
  lastError: null,
  log: []
};

function appendRuntimeLog(message: string): void {
  const line = message.trim();
  if (!line) return;
  runtimeState.log.push(line);
  runtimeState.log = runtimeState.log.slice(-200);
  mainWindow?.webContents.send("runtime:log", line);
}

function repoRoot(): string {
  return path.resolve(__dirname, "../../..");
}

function runtimeResourceDir(): string {
  return isDev
    ? repoRoot()
    : path.join(process.resourcesPath, "runtime");
}

function packagedRuntimeExe(): string {
  return path.join(runtimeResourceDir(), "flowpilot-runtime.exe");
}

function runtimeCommand(): { command: string; args: string[]; cwd: string } {
  const cwd = runtimeResourceDir();
  if (!isDev && fs.existsSync(packagedRuntimeExe())) {
    return { command: packagedRuntimeExe(), args: [], cwd };
  }

  const embeddedPython = path.join(cwd, "python", "python.exe");
  if (!isDev && fs.existsSync(embeddedPython)) {
    return { command: embeddedPython, args: ["-m", "runtime.api"], cwd };
  }

  if (isDev) {
    return { command: "uv", args: ["run", "flowpilot-runtime"], cwd };
  }

  return { command: "python", args: ["-m", "runtime.api"], cwd };
}

async function waitForPort(port: number, timeoutMs = 15000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const connected = await new Promise<boolean>((resolve) => {
      const socket = net.createConnection({ host: "127.0.0.1", port });
      socket.once("connect", () => {
        socket.end();
        resolve(true);
      });
      socket.once("error", () => resolve(false));
      socket.setTimeout(500, () => {
        socket.destroy();
        resolve(false);
      });
    });
    if (connected) return;
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  throw new Error(`Runtime did not listen on port ${port} within ${timeoutMs}ms`);
}

async function startRuntime(): Promise<RuntimeState> {
  if (runtimeProcess && !runtimeProcess.killed) {
    return runtimeState;
  }

  runtimeState.ready = false;
  runtimeState.lastError = null;
  const command = runtimeCommand();
  appendRuntimeLog(`Starting runtime: ${command.command} ${command.args.join(" ")}`);

  runtimeProcess = spawn(command.command, command.args, {
    cwd: command.cwd,
    env: {
      ...process.env,
      FLOWPILOT_RUNTIME_TOKEN: runtimeToken,
      FLOWPILOT_RUNTIME_PORT: String(runtimePort),
      DESKTOP_AGENT_CONFIG: path.join(command.cwd, "config.yaml"),
      PYTHONUTF8: "1"
    },
    windowsHide: true
  });
  runtimeState.pid = runtimeProcess.pid ?? null;

  runtimeProcess.stdout.on("data", (chunk) => appendRuntimeLog(String(chunk)));
  runtimeProcess.stderr.on("data", (chunk) => appendRuntimeLog(String(chunk)));
  runtimeProcess.on("exit", (code, signal) => {
    appendRuntimeLog(`Runtime exited: code=${code ?? "null"} signal=${signal ?? "null"}`);
    runtimeState.ready = false;
    runtimeState.pid = null;
    runtimeProcess = null;
    mainWindow?.webContents.send("runtime:state", runtimeState);
  });
  runtimeProcess.on("error", (error) => {
    runtimeState.lastError = error.message;
    appendRuntimeLog(`Runtime process error: ${error.message}`);
  });

  try {
    await waitForPort(runtimePort);
    runtimeState.ready = true;
  } catch (error) {
    runtimeState.lastError = error instanceof Error ? error.message : String(error);
  }
  return runtimeState;
}

function stopRuntime(): void {
  if (!runtimeProcess) return;
  runtimeProcess.kill();
  runtimeProcess = null;
  runtimeState.ready = false;
  runtimeState.pid = null;
}

async function createWindow(): Promise<void> {
  mainWindow = new BrowserWindow({
    width: 1240,
    height: 820,
    minWidth: 1040,
    minHeight: 680,
    title: "SEWC FlowPilot",
    backgroundColor: "#f5f7f8",
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false
    }
  });

  if (isDev && process.env.VITE_DEV_SERVER_URL) {
    await mainWindow.loadURL(process.env.VITE_DEV_SERVER_URL);
  } else {
    await mainWindow.loadFile(path.join(__dirname, "../renderer/index.html"));
  }
}

ipcMain.handle("runtime:getState", () => runtimeState);
ipcMain.handle("runtime:start", () => startRuntime());
ipcMain.handle("runtime:stop", () => {
  stopRuntime();
  return runtimeState;
});

ipcMain.handle("runtime:request", async (_event, input: {
  path: string;
  method?: string;
  body?: unknown;
}) => {
  const response = await fetch(`${runtimeState.baseUrl}${input.path}`, {
    method: input.method || "GET",
    headers: {
      "Content-Type": "application/json",
      "X-Runtime-Token": runtimeToken
    },
    body: input.body === undefined ? undefined : JSON.stringify(input.body)
  });
  const text = await response.text();
  let data: unknown = text;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }
  if (!response.ok) {
    return { ok: false, status: response.status, data };
  }
  return { ok: true, status: response.status, data };
});

app.whenReady().then(async () => {
  await startRuntime();
  await createWindow();
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
  }
});

app.on("before-quit", () => {
  stopRuntime();
});
