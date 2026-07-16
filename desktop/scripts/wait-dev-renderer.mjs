const rendererUrl = "http://127.0.0.1:5173/main.ts";
const requiredMarkers = ["scheduled-page", "task-layout", "new-chat-button"];
const timeoutMs = 15000;
const startedAt = Date.now();

async function delay(ms) {
  await new Promise((resolve) => setTimeout(resolve, ms));
}

while (Date.now() - startedAt < timeoutMs) {
  try {
    const response = await fetch(rendererUrl, { cache: "no-store" });
    if (response.ok) {
      const source = await response.text();
      const missing = requiredMarkers.filter((marker) => !source.includes(marker));
      if (missing.length === 0) {
        console.log(`Renderer dev server verified at ${rendererUrl}`);
        process.exit(0);
      }

      console.error(
        "Vite is responding on 127.0.0.1:5173, but it is not serving the current FlowPilot renderer source."
      );
      console.error(`Missing markers: ${missing.join(", ")}`);
      console.error("Stop the old dev server or run npm run dev from the updated desktop directory.");
      process.exit(1);
    }
  } catch {
    await delay(300);
    continue;
  }
  await delay(300);
}

console.error(`Timed out waiting for current renderer dev server at ${rendererUrl}`);
process.exit(1);
