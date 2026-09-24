import { expect, test, type Page } from "@playwright/test";
const workspace = {
  id: "ws-1",
  name: "Alpine archive",
  root: "/Projects/alpine-archive",
  enabled: true,
  agent_enabled: true,
  write_scope: "handoff",
  excludes: "[]",
  runtime_grants: {
    pi: { enabled: true, profile: { id: "read-only" } },
    codex: { enabled: false, profile: null },
  },
};
const run = {
  run_id: "run-1",
  runtime: "pi",
  phase: "active",
  active_state: "waiting_interaction",
  handoff_title: "Review project structure",
  workspace_name: "Alpine archive",
  workspace_id: "ws-1",
  model: "muse-spark",
  created: "2026-09-24T10:00:00Z",
  conversation_id: "conv-1",
};
const piReadOnlyConfig = {
  version: 3,
  write_tools_enabled: false,
  tools: { read: "allow", grep: "allow", find: "allow", ls: "allow",
    edit: "ask", write: "ask" },
  protected_patterns: [".git/**", ".env", ".env.*"],
  protected_template_exceptions: [".env.example", ".env.sample", ".env.template"],
  allow_session_always: true,
  external_access: { default_mode: "deny", roots: [] },
  shell_mode: "deny",
};
async function mockApi(page: Page) {
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const data: Record<string, unknown> = {
      "/api/status": {
        version: "0.8.4",
        listen_mode: "loopback",
        allowed_parents: ["/Projects"],
        mcp_port: 8765,
        bridge: { configured: true, enabled: true },
        runtimes: {
          runtimes: {
            pi: {
              configured: true,
              healthy: true,
              protocol: 1,
              native_version: "0.87.0",
            },
            codex: { configured: true, healthy: false, protocol: 1 },
          },
        },
        runtime_policies: {
          pi: {
            configured: true,
            enabled: ["muse-spark"],
            default: "muse-spark",
            enabled_count: 1,
          },
        },
      },
      "/api/workspaces": { workspaces: [workspace] },
      "/api/events": {
        events: [
          {
            at: "2026-09-24T10:00:00Z",
            workspace: "ws-1",
            action: "enable",
            outcome: "ok",
          },
        ],
      },
      "/api/runs": { runs: [run], next_offset: null },
      "/api/workspaces/ws-1/jobs": {
        handoffs: [
          {
            id: "job-1",
            title: "Review project structure",
            state: "prepared",
            path: "/Projects/alpine-archive/.workspace-handoff/job-1",
            copy_prompt: "Review the structure",
          },
        ],
      },
      "/api/workspaces/ws-1/runs": { runs: [run] },
      "/api/runs/run-1": {
        ...run,
        interactions: [
          {
            id: "req-1",
            kind: "choice",
            state: "pending",
            details: {
              title: "Allow file read?",
              resource: "src/main.py",
              choices: [
                { id: "approve", label: "Allow once", semantic: "approve" },
                { id: "reject", label: "Reject" },
              ],
            },
          },
        ],
      },
      "/api/runs/run-1/activities": { activities: [] },
      "/api/runs/run-1/executions": { executions: [] },
      "/api/runtimes/pi/models": {
        models: [{ selector: "muse-spark", displayName: "Muse Spark" }],
        policy: {
          configured: true,
          enabled: ["muse-spark"],
          default: "muse-spark",
        },
      },
      "/api/runtimes/pi/profiles": {
        profiles: [{ id: "read-only", revision: "default-revision", mutable: false,
          config: piReadOnlyConfig }],
      },
    };
    const body =
      path === "/api/runtimes/pi/models"
        ? data[path]
        : (data[path] ?? { ok: true });
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(body),
    });
  });
}
test("desktop operations and request review", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await mockApi(page);
  await page.goto("./");
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
  await expect(page.getByText("1 run needs review")).toBeVisible();
  await page.screenshot({
    path: "test-results/desktop-overview.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "Workspaces", exact: true }).click();
  await expect(page.getByText("Alpine archive").first()).toBeVisible();
  await page.screenshot({
    path: "test-results/desktop-workspaces.png",
    fullPage: true,
  });
  await page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("button", { name: "Handoffs" })
    .click();
  await page.getByLabel("Workspace", { exact: true }).selectOption("ws-1");
  await expect(
    page.getByText("Review project structure").first(),
  ).toBeVisible();
  await page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("button", { name: /Runs/ })
    .click();
  await page.getByRole("button", { name: "Review run" }).click();
  await expect(page.getByText("Allow file read?")).toBeVisible();
  await page.screenshot({
    path: "test-results/desktop-runs.png",
    fullPage: true,
  });
  expect(errors).toEqual([]);
});
test("workspace settings use one save and ID copy sits by the name", async ({
  page,
}) => {
  await mockApi(page);
  const saves: unknown[] = [];
  await page.route("**/api/workspaces/ws-1", async (route) => {
    saves.push(route.request().postDataJSON());
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ workspace }),
    });
  });
  await page.goto("./");
  await page.getByRole("button", { name: "Workspaces", exact: true }).click();
  const card = page.locator(".workspace-entry");
  const name = card.locator(".workspace-name-line");
  await expect(
    name.getByRole("heading", { name: "Alpine archive" }),
  ).toBeVisible();
  await expect(
    name.getByRole("button", { name: "Copy workspace ID for Alpine archive" }),
  ).toBeVisible();
  await card.getByRole("button", { name: /Settings/ }).click();
  await page.screenshot({
    path: "test-results/workspace-settings.png",
    fullPage: true,
  });
  await expect(
    card.getByRole("button", { name: "Save settings" }),
  ).toBeDisabled();
  await card.getByLabel("File write access").selectOption("none");
  await card.getByLabel("Extra exclusions").fill("private/**");
  await expect(
    card.getByRole("button", { name: "Save settings" }),
  ).toBeEnabled();
  await card.getByRole("button", { name: "Save settings" }).click();
  await page.getByRole("button", { name: "Continue" }).click();
  await expect.poll(() => saves.length).toBe(1);
  expect(saves[0]).toEqual({
    operation: "set_settings",
    write_scope: "none",
    excludes: ["private/**"],
  });
});
test("custom Pi profile builder creates, edits and deletes external access rules", async ({ page }) => {
  await mockApi(page);
  const profileRows: Array<{ id: string; revision: string; mutable: boolean;
    config: Record<string, unknown> }> = [
    { id: "read-only", revision: "default-revision", mutable: false,
      config: piReadOnlyConfig },
  ];
  const assignments: unknown[] = [];
  const saved: unknown[] = [];
  await page.route("**/api/runtimes/pi/profiles", async (route) => {
    if (route.request().method() === "POST") {
      const body = route.request().postDataJSON();
      saved.push(body);
      const row = { id: body.id, revision: `revision-${saved.length}`,
        mutable: true, config: body.config };
      const index = profileRows.findIndex((profile) => profile.id === row.id);
      if (index >= 0) profileRows[index] = row;
      else profileRows.push(row);
      return route.fulfill({ status: 200, contentType: "application/json",
        body: JSON.stringify(row) });
    }
    await route.fulfill({ status: 200, contentType: "application/json",
      body: JSON.stringify({ profiles: profileRows }) });
  });
  await page.route("**/api/runtimes/pi/profiles/*", async (route) => {
    const id = new URL(route.request().url()).pathname.split("/").pop();
    const index = profileRows.findIndex((profile) => profile.id === id);
    if (index >= 0) profileRows.splice(index, 1);
    await route.fulfill({ status: 200, contentType: "application/json",
      body: JSON.stringify({ deleted: id }) });
  });
  await page.route("**/api/workspaces/ws-1/runtimes/pi", async (route) => {
    assignments.push(route.request().postDataJSON());
    await route.fulfill({ status: 200, contentType: "application/json",
      body: "{}" });
  });
  await page.goto("./");
  await page.getByRole("button", { name: "Profiles", exact: true }).click();
  await page.getByRole("button", { name: "Create from selected" }).click();
  await page.getByLabel("Profile ID").fill("reviewed-shared");
  await page.getByLabel("Enable edit and write").check();
  await page.getByLabel("Default file access").selectOption("deny");
  await page.getByRole("button", { name: "Add external root" }).click();
  await page.getByRole("textbox", { name: "External root 1", exact: true }).fill("/Volumes/shared");
  await page.getByLabel("Access for external root 1").selectOption("ask");
  await page.screenshot({ path: "test-results/profile-editor-desktop.png", fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: "test-results/profile-editor-mobile.png", fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.getByRole("button", { name: "Create profile" }).click();
  await expect.poll(() => saved.length).toBe(1);
  expect((saved[0] as { config: typeof piReadOnlyConfig }).config.external_access.roots)
    .toEqual([{ path: "/Volumes/shared", mode: "ask" }]);

  await page.getByRole("button", { name: "Workspaces", exact: true }).click();
  await page.getByRole("button", { name: "Change profile" }).first().click();
  await page.getByRole("button", { name: /reviewed-shared/ }).click();
  await page.getByRole("button", { name: "Assign profile" }).click();
  await expect.poll(() => assignments.length).toBe(1);
  expect(assignments[0]).toEqual({ enabled: true, profile_id: "reviewed-shared" });

  await page.getByRole("button", { name: "Profiles", exact: true }).click();
  await page.getByRole("button", { name: /reviewed-shared/ }).click();
  await page.getByRole("button", { name: "Edit controls" }).click();
  await page.getByLabel("Shell commands").selectOption("ask");
  await page.getByRole("button", { name: "Save controls" }).click();
  await expect.poll(() => saved.length).toBe(2);
  expect((saved[1] as { expected_revision: string }).expected_revision)
    .toBe("revision-1");

  await page.getByRole("button", { name: "Workspaces", exact: true }).click();
  await page.getByRole("button", { name: "Change profile" }).first().click();
  await page.getByRole("button", { name: /read-only/ }).click();
  await page.getByRole("button", { name: "Assign profile" }).click();
  await expect.poll(() => assignments.length).toBe(2);

  await page.getByRole("button", { name: "Profiles", exact: true }).click();
  await page.getByRole("button", { name: /reviewed-shared/ }).click();
  await page.getByRole("button", { name: "Delete", exact: true }).click();
  await page.getByRole("button", { name: "Delete profile" }).click();
  await expect.poll(() => profileRows.some((profile) => profile.id === "reviewed-shared"))
    .toBe(false);
});
test("mobile navigation, models, and no overflow", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await mockApi(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("./");
  await page.screenshot({
    path: "test-results/mobile-overview.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "Open navigation" }).click();
  await page.getByRole("button", { name: "Runtimes", exact: true }).click();
  await expect(page.getByText("Bridge connection")).toBeVisible();
  await page.getByRole("button", { name: "Manage models" }).first().click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.screenshot({
    path: "test-results/mobile-models.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "Cancel" }).click();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.setViewportSize({ width: 320, height: 700 });
  await page.getByRole("button", { name: "Open navigation" }).click();
  await page.getByRole("button", { name: "Workspaces", exact: true }).click();
  await page.getByRole("button", { name: /Settings/ }).click();
  await page.screenshot({
    path: "test-results/mobile-workspace-settings.png",
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  expect(errors).toEqual([]);
});

test("login keeps token out of storage and returns to locked view", async ({
  page,
}) => {
  let signedIn = false;
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/status" && !signedIn)
      return route.fulfill({
        status: 401,
        contentType: "application/json",
        body: '{"error":"Admin token required"}',
      });
    if (path === "/api/login") {
      signedIn =
        route.request().headers().authorization === "Bearer current-token";
      return route.fulfill({
        status: signedIn ? 200 : 401,
        contentType: "application/json",
        body: signedIn ? '{"status":"ok"}' : '{"error":"Admin token required"}',
      });
    }
    if (path === "/api/logout") {
      signedIn = false;
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: "{}",
      });
    }
    const data =
      path === "/api/status"
        ? {
            version: "0.8.4",
            listen_mode: "loopback",
            allowed_parents: ["/Projects"],
            mcp_port: 8765,
            bridge: { configured: false, enabled: false },
            runtimes: { runtimes: {} },
            runtime_policies: {},
          }
        : path === "/api/workspaces"
          ? { workspaces: [] }
          : path === "/api/events"
            ? { events: [] }
            : path === "/api/runs"
              ? { runs: [], next_offset: null }
              : {};
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(data),
    });
  });
  await page.goto("./");
  await expect(
    page.getByRole("heading", { name: "Open the console" }),
  ).toBeVisible();
  await page.getByLabel("Admin token").fill("wrong-token");
  await page.getByRole("button", { name: "Open manager" }).click();
  await expect(page.getByRole("alert")).toContainText("not current");
  await page.getByLabel("Admin token").fill("current-token");
  await page.getByRole("button", { name: "Open manager" }).click();
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
  expect(
    await page.evaluate(() =>
      Object.values(localStorage).some((v) => v.includes("current-token")),
    ),
  ).toBe(false);
  await page.getByRole("button", { name: "Lock console" }).click();
  await expect(
    page.getByRole("heading", { name: "Open the console" }),
  ).toBeVisible();
});
