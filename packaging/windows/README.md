# Windows Test Build Runtime Injection

The Electron shell expects Python runtime resources under:

```text
desktop/resources/runtime/
```

For development:

```powershell
cd desktop
npm run stage:runtime
npm run dev
```

For a Windows user-test package, use one of these runtime layouts:

## Option A: PyInstaller Runtime

Build a `flowpilot-runtime.exe` from the repository root and place it at:

```text
desktop/resources/runtime/flowpilot-runtime.exe
```

The Electron main process will prefer that executable in packaged builds.

The executable must include or be able to import:

- `runtime.api`
- all project packages (`agent`, `tools`, `skills`, `memory`, etc.)
- WinPeekaboo as `python -m winpeekaboo`
- Playwright Python package and browser driver support

## Option B: Embedded Python Runtime

Place an embedded Python distribution at:

```text
desktop/resources/runtime/python/python.exe
```

Then ensure the staged runtime directory contains installed dependencies and
WinPeekaboo is importable. In packaged builds Electron will run:

```powershell
python\python.exe -m runtime.api
```

## Required Runtime Environment

The Electron app injects these environment variables when launching Runtime:

```text
FLOWPILOT_RUNTIME_TOKEN=<per-launch random token>
FLOWPILOT_RUNTIME_PORT=8765
DESKTOP_AGENT_CONFIG=<runtime>/config.yaml
PYTHONUTF8=1
```

## WinPeekaboo Injection

The current Python adapter calls:

```powershell
python -m winpeekaboo ...
```

For test builds, validate this inside the packaged runtime before distributing:

```powershell
cd desktop\resources\runtime
python\python.exe -m winpeekaboo list windows --json
```

If using PyInstaller, run the equivalent command through the packaged runtime
or add a small smoke-test command to the build pipeline.

## Build Shell

```powershell
cd desktop
npm install
npm run stage:runtime
npm run pack:win
```

This creates a Windows installer under `desktop/out/`.
