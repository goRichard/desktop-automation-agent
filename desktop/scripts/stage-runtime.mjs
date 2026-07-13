import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const desktopRoot = path.resolve(__dirname, "..");
const repoRoot = path.resolve(desktopRoot, "..");
const output = path.join(desktopRoot, "resources", "runtime");

const copyEntries = [
  "agent",
  "cli",
  "config",
  "credentials",
  "llm",
  "memory",
  "runtime",
  "scheduler",
  "skills",
  "tasks",
  "tools",
  "main.py",
  "pyproject.toml",
  "uv.lock",
  "config.yaml",
  ".env.example"
];

const optionalEntries = [
  "winpeekaboo",
  "siemens_ca_chain.pem"
];

function remove(target) {
  fs.rmSync(target, { recursive: true, force: true });
}

function copy(src, dst) {
  if (!fs.existsSync(src)) return false;
  const stat = fs.statSync(src);
  if (stat.isDirectory()) {
    fs.mkdirSync(dst, { recursive: true });
    for (const child of fs.readdirSync(src)) {
      if (child === "__pycache__" || child === ".pytest_cache" || child === ".ruff_cache") {
        continue;
      }
      copy(path.join(src, child), path.join(dst, child));
    }
    return true;
  }
  fs.mkdirSync(path.dirname(dst), { recursive: true });
  fs.copyFileSync(src, dst);
  return true;
}

remove(output);
fs.mkdirSync(output, { recursive: true });

for (const entry of copyEntries) {
  const copied = copy(path.join(repoRoot, entry), path.join(output, entry));
  if (!copied) {
    throw new Error(`Required runtime entry is missing: ${entry}`);
  }
}

for (const entry of optionalEntries) {
  copy(path.join(repoRoot, entry), path.join(output, entry));
}

fs.mkdirSync(path.join(output, "data"), { recursive: true });

const marker = {
  stagedAt: new Date().toISOString(),
  source: repoRoot,
  notes: [
    "This is a source runtime staging folder for Electron packaging.",
    "For a user-test Windows build, place flowpilot-runtime.exe here or add python/python.exe.",
    "WinPeekaboo must be importable by the bundled runtime as python -m winpeekaboo."
  ]
};
fs.writeFileSync(path.join(output, "STAGED_RUNTIME.json"), JSON.stringify(marker, null, 2));

console.log(`Runtime staged at ${output}`);
