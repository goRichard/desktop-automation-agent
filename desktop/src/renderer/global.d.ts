export {};

declare global {
  interface Window {
    flowpilot: {
      getRuntimeState: () => Promise<RuntimeState>;
      startRuntime: () => Promise<RuntimeState>;
      stopRuntime: () => Promise<RuntimeState>;
      request: <T = unknown>(input: RuntimeRequest) => Promise<ApiResult<T>>;
      onRuntimeLog: (callback: (line: string) => void) => () => void;
      onRuntimeState: (callback: (state: RuntimeState) => void) => () => void;
    };
  }
}

export type RuntimeState = {
  baseUrl: string;
  token: string;
  pid: number | null;
  ready: boolean;
  source: "dev" | "packaged";
  lastError: string | null;
  log: string[];
};

export type RuntimeRequest = {
  path: string;
  method?: string;
  body?: unknown;
};

export type ApiResult<T> = {
  ok: boolean;
  status: number;
  data: T;
};
