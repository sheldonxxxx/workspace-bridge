import { expect, test } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

function builtManagerRelease() {
  const file = path.resolve(
    import.meta.dirname,
    "../../workspace_bridge/static/dist/release.json",
  );
  return JSON.parse(fs.readFileSync(file, "utf8"));
}

async function mockRelease(page: any, managerRelease: any) {
  await page.route("**/api/**", async (route: any) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/account") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          username: "admin",
          must_change_password: false,
        }),
      });
      return;
    }
    if (url.pathname === "/api/status") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          version: "0.1.2",
          release: {
            contract: 1,
            product: "workspace-bridge",
            product_version: "0.1.2",
            component: "bridge",
            component_version: "0.1.2",
            build_id: "sha256:" + "a".repeat(64),
          },
          manager_release: managerRelease,
          listen_mode: "loopback",
          mcp_port: 8765,
          bridge: { configured: true, enabled: true },
          nodes: [],
          adapters: { adapters: [] },
        }),
      });
      return;
    }
    if (url.pathname === "/api/diagnostics") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          generated_at: new Date().toISOString(),
          mode: "live",
          overall: {
            status: "pass",
            summary: "All observed local and adapter checks passed.",
            counts: {
              pass: 1,
              warning: 0,
              unknown: 0,
              action_required: 0,
              failed: 0,
            },
          },
          checks: [],
          runnable_routes: [],
          release: {},
        }),
      });
      return;
    }
    if (url.pathname === "/api/workspaces") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ workspaces: [] }),
      });
      return;
    }
    if (url.pathname === "/api/events") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ events: [] }),
      });
      return;
    }
    if (url.pathname === "/api/runs") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ runs: [], next_offset: null }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({}),
    });
  });
}

test("same-build Manager shows release without mismatch", async ({ page }) => {
  const built = builtManagerRelease();
  expect(built.component).toBe("manager");
  expect(built.product_version).toBe("0.1.2");
  await mockRelease(page, built);
  await page.goto("./");
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
  await expect(page.getByText("Bridge release 0.1.2").first()).toBeVisible();
  await expect(page.getByText("Manager release 0.1.2").first()).toBeVisible();
  await expect(
    page.getByText("Manager build mismatch / refresh or rebuild required"),
  ).toHaveCount(0);
  await expect(page.getByText("Manager identity unavailable")).toHaveCount(0);
});

test("stale cached Manager warns without destroying data", async ({ page }) => {
  const built = builtManagerRelease();
  const stale = { ...built, build_id: `sha256:${"b".repeat(64)}` };
  expect(stale.build_id).not.toBe(built.build_id);
  await mockRelease(page, stale);
  await page.goto("./");
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
  await expect(
    page.getByText("Manager build mismatch / refresh or rebuild required"),
  ).toBeVisible();
  await expect(page.getByText("Manager identity unavailable")).toHaveCount(0);
});

test("same build ID with different product version still warns", async ({
  page,
}) => {
  const built = builtManagerRelease();
  const skewed = {
    ...built,
    product_version: "0.1.0",
    component_version: "0.1.0",
  };
  expect(skewed.build_id).toBe(built.build_id);
  expect(skewed.product_version).not.toBe(built.product_version);
  await mockRelease(page, skewed);
  await page.goto("./");
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
  await expect(
    page.getByText("Manager build mismatch / refresh or rebuild required"),
  ).toBeVisible();
  await expect(page.getByText("Manager identity unavailable")).toHaveCount(0);
});

test("missing Manager identity is unavailable, not a false mismatch", async ({
  page,
}) => {
  await mockRelease(page, null);
  await page.goto("./");
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
  await expect(page.getByText("Manager identity unavailable")).toBeVisible();
  await expect(
    page.getByText("Manager build mismatch / refresh or rebuild required"),
  ).toHaveCount(0);
});
