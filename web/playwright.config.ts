import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./tests",
  workers: process.env.CI ? 1 : undefined,
  use: {
    baseURL: "http://127.0.0.1:5187/static/dist/",
    browserName: "chromium",
    channel: process.env.CI ? undefined : "chrome",
  },
  webServer: {
    command: "npm run dev -- --host 127.0.0.1 --port 5187",
    url: "http://127.0.0.1:5187/static/dist/",
    reuseExistingServer: false,
    timeout: 30_000,
  },
});
