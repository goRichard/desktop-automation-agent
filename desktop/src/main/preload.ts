import { contextBridge, ipcRenderer } from "electron";

type RuntimeRequest = {
  path: string;
  method?: string;
  body?: unknown;
};

contextBridge.exposeInMainWorld("flowpilot", {
  getRuntimeState: () => ipcRenderer.invoke("runtime:getState"),
  startRuntime: () => ipcRenderer.invoke("runtime:start"),
  stopRuntime: () => ipcRenderer.invoke("runtime:stop"),
  request: (input: RuntimeRequest) => ipcRenderer.invoke("runtime:request", input),
  onRuntimeLog: (callback: (line: string) => void) => {
    const listener = (_event: unknown, line: string) => callback(line);
    ipcRenderer.on("runtime:log", listener);
    return () => ipcRenderer.removeListener("runtime:log", listener);
  },
  onRuntimeState: (callback: (state: unknown) => void) => {
    const listener = (_event: unknown, state: unknown) => callback(state);
    ipcRenderer.on("runtime:state", listener);
    return () => ipcRenderer.removeListener("runtime:state", listener);
  }
});
