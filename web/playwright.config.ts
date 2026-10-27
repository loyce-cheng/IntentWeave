import { defineConfig } from "@playwright/test";
import path from "node:path";
const root = path.resolve(import.meta.dirname, "..");
const python =
  process.env.WORLDMIND_PYTHON ||
  path.join(
    root,
    ".venv",
    process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
  );
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 30000,
  expect: { timeout: 10000 },
  use: {
    baseURL: "http://127.0.0.1:8000",
    viewport: { width: 1440, height: 1000 },
    launchOptions: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
      ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE }
      : {},
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  reporter: [["list"], ["json", { outputFile: "test-results/results.json" }]],
  webServer: {
    command: `"${python}" scripts/e2e_server.py`,
    cwd: root,
    url: "http://127.0.0.1:8000/api/health",
    reuseExistingServer: false,
    gracefulShutdown: { signal: "SIGINT", timeout: 5000 },
    timeout: 30000,
  },
});
