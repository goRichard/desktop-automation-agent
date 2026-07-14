import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const desktopRoot = path.resolve(__dirname, "..");
const output = path.join(desktopRoot, "dist", "main", "preload.cjs");

fs.mkdirSync(path.dirname(output), { recursive: true });
fs.writeFileSync(output, `const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("flowpilot", {
  getRuntimeState: () => ipcRenderer.invoke("runtime:getState"),
  startRuntime: () => ipcRenderer.invoke("runtime:start"),
  stopRuntime: () => ipcRenderer.invoke("runtime:stop"),
  request: (input) => ipcRenderer.invoke("runtime:request", input),
  onRuntimeLog: (callback) => {
    const listener = (_event, line) => callback(line);
    ipcRenderer.on("runtime:log", listener);
    return () => ipcRenderer.removeListener("runtime:log", listener);
  },
  onRuntimeState: (callback) => {
    const listener = (_event, state) => callback(state);
    ipcRenderer.on("runtime:state", listener);
    return () => ipcRenderer.removeListener("runtime:state", listener);
  }
});
`);

console.log(`Preload written to ${output}`);
