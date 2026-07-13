# SEWC FlowPilot Desktop Shell

Minimal Electron shell for testing the Python Runtime on Windows.

## Development

```powershell
cd desktop
npm install
npm run stage:runtime
npm run dev
```

In development, Electron starts the Runtime with:

```powershell
uv run flowpilot-runtime
```

from the repository root. The Runtime listens on `127.0.0.1:8765` with a
per-launch token injected by Electron.

## Windows Test Package

```powershell
cd desktop
npm install
npm run stage:runtime
npm run pack:win
```

The staged runtime is copied into the installer as `resources/runtime`.

For a real user-test package, add one of these before `npm run pack:win`:

- `desktop/resources/runtime/flowpilot-runtime.exe`, built from the Python Runtime.
- `desktop/resources/runtime/python/python.exe`, with dependencies installed beside it.

WinPeekaboo must be importable by that runtime as:

```powershell
python -m winpeekaboo list windows --json
```

See [../packaging/windows/README.md](../packaging/windows/README.md) for the runtime
injection contract.

## Current UI

- Runtime status and logs.
- Chat-style Agent Run creation through `POST /runs`.
- Run list, Run detail, step status, output and events.
- Confirmation approve/reject for `waiting_user` runs.
- Skill list.
- Chat model configuration health check.

This is intentionally a technical preview shell, not the final product UI.
