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
    pi: { enabled: true, profile: { id: "read-only" }, security_binding: null },
    codex: { enabled: false, profile: null, security_binding: null },
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
const diagnosticsReport = {
  generated_at: "2026-09-25T08:00:00Z",
  mode: "live",
  overall: {
    status: "unknown",
    summary: "Some runtime freshness was not observed.",
    counts: { pass: 8, warning: 1, unknown: 1, action_required: 0, failed: 0 },
  },
  checks: [
    {
      id: "conversation.security_update_pending:ws-1:pi",
      code: "conversation.security_update_pending",
      section: "models_profiles",
      status: "warning",
      summary:
        "A prior conversation will refresh its security settings before its next turn.",
      workspace_id: "ws-1",
      runtime: "pi",
    },
  ],
  runnable_routes: [
    {
      id: "ws-1:pi",
      workspace_id: "ws-1",
      workspace_name: "Alpine archive",
      runtime: "pi",
      ready: true,
      status: "ready",
      summary: "This exact workspace/runtime path is ready to start.",
      blockers: [],
      profile: { id: "read-only", revision: "default-revision" },
      default_model_selector: "muse-spark",
    },
  ],
};
function runnableRoute(
  workspaceId: string,
  workspaceName: string,
  runtime: string,
  ready: boolean,
  blockers: string[] = [],
) {
  return {
    id: `${workspaceId}:${runtime}`,
    workspace_id: workspaceId,
    workspace_name: workspaceName,
    runtime,
    ready,
    status: ready ? "ready" : "blocked",
    summary: ready
      ? "This exact workspace/runtime path is ready to start."
      : `Runnable path has ${blockers.length} blocker(s).`,
    blockers,
    profile: runtime === "pi" ? { id: "read-only" } : null,
    default_model_selector: runtime === "pi" ? "muse-spark" : "gpt-test",
  };
}
function reportWith(
  routes: unknown[],
  checks: unknown[] = [],
  status = "unknown",
) {
  return {
    ...diagnosticsReport,
    overall: { ...diagnosticsReport.overall, status },
    checks: [...diagnosticsReport.checks, ...checks],
    runnable_routes: routes,
  };
}
const piReadOnlyConfig = {
  version: 3,
  write_tools_enabled: false,
  tools: {
    read: "allow",
    grep: "allow",
    find: "allow",
    ls: "allow",
    edit: "ask",
    write: "ask",
  },
  protected_patterns: [".git/**", ".env", ".env.*"],
  protected_template_exceptions: [
    ".env.example",
    ".env.sample",
    ".env.template",
  ],
  allow_session_always: true,
  external_access: { default_mode: "deny", roots: [] },
  shell_mode: "deny",
};
async function mockApi(
  page: Page,
  options: { diagnostics?: unknown; workspaces?: unknown; runs?: unknown } = {},
) {
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
      "/api/diagnostics": options.diagnostics ?? diagnosticsReport,
      "/api/workspaces": options.workspaces ?? { workspaces: [workspace] },
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
      "/api/runs": options.runs ?? { runs: [run], next_offset: null },
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
        profiles: [
          {
            id: "read-only",
            revision: "default-revision",
            mutable: false,
            config: piReadOnlyConfig,
          },
        ],
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
  await expect(page.getByText("System health").first()).toBeVisible();
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
test("canonical Ready route stays startable while overall health is unknown", async ({
  page,
}) => {
  await mockApi(page);
  let request: Record<string, unknown> | undefined;
  await page.route("**/api/workspaces/ws-1/jobs/job-1/runs", async (route) => {
    request = route.request().postDataJSON();
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ...run,
        run_id: "run-started",
        phase: "active",
        active_state: "running",
        model: "muse-spark",
      }),
    });
  });
  await page.goto("./");
  await expect(page.getByText("Health unknown")).toBeVisible();
  await expect(
    page.getByText(
      "A prior conversation will refresh its security settings before its next turn.",
    ),
  ).toBeVisible();
  await page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("button", { name: "Handoffs" })
    .click();
  await page.getByLabel("Workspace", { exact: true }).selectOption("ws-1");
  await expect(page.getByText("Default model: muse-spark")).toBeVisible();
  const start = page.getByRole("button", { name: "Start run" });
  await expect(start).toBeEnabled();
  await start.click();
  await expect(page.getByRole("heading", { name: "Agent runs" })).toBeVisible();
  expect(request).toMatchObject({ runtime: "pi" });
  expect(Object.keys(request || {}).sort()).toEqual(["request_id", "runtime"]);
  expect(request?.request_id).toMatch(/^[a-zA-Z0-9_-]{1,64}$/);
});
test("globally complete setup facts cannot override a blocked exact route", async ({
  page,
}) => {
  const report = reportWith(
    [
      runnableRoute("ws-1", "Alpine archive", "pi", false, [
        "model.default_freshness",
      ]),
    ],
    [
      {
        id: "model.default_freshness:ws-1:pi",
        code: "model.default_freshness",
        section: "models_profiles",
        status: "action_required",
        summary: "Configured default model is unavailable for this workspace.",
        remediation:
          "Choose a currently discovered model as the runtime default.",
        workspace_id: "ws-1",
        runtime: "pi",
      },
    ],
  );
  await mockApi(page, {
    diagnostics: report,
    runs: { runs: [], next_offset: null },
  });
  await page.goto("./");
  await expect(page.getByText("System health").first()).toBeVisible();
  await page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("button", { name: "Workspaces", exact: true })
    .click();
  await expect(
    page.locator(".runtime-access").getByText("Blocked"),
  ).toBeVisible();
  await page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("button", { name: "Handoffs" })
    .click();
  await page.getByLabel("Workspace", { exact: true }).selectOption("ws-1");
  await expect(
    page.getByText(
      "Choose a currently discovered model as the runtime default.",
    ),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Start run" })).toBeDisabled();
});
test("workspace and runtime route status never cross-combines", async ({
  page,
}) => {
  const beta = {
    ...workspace,
    id: "ws-2",
    name: "Beta archive",
    root: "/Projects/beta-archive",
    runtime_grants: {
      pi: {
        enabled: false,
        profile: { id: "read-only" },
        security_binding: null,
      },
      codex: { enabled: true, profile: null, security_binding: null },
    },
  };
  const report = reportWith([
    runnableRoute("ws-1", "Alpine archive", "pi", true),
    runnableRoute("ws-1", "Alpine archive", "codex", false, [
      "runtime.reachable",
    ]),
    runnableRoute("ws-2", "Beta archive", "pi", false, ["profile.freshness"]),
    runnableRoute("ws-2", "Beta archive", "codex", true),
  ]);
  await mockApi(page, {
    diagnostics: report,
    workspaces: { workspaces: [workspace, beta] },
    runs: { runs: [], next_offset: null },
  });
  await page.goto("./");
  await expect(page.getByText("System health").first()).toBeVisible();
  await page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("button", { name: "Workspaces", exact: true })
    .click();
  const cards = page.locator(".workspace-entry");
  await expect(
    cards.nth(0).locator(".runtime-access").nth(0).getByText("Ready"),
  ).toBeVisible();
  await expect(
    cards.nth(0).locator(".runtime-access").nth(1).getByText("Blocked"),
  ).toBeVisible();
  await expect(
    cards.nth(1).locator(".runtime-access").nth(0).getByText("Blocked"),
  ).toBeVisible();
  await expect(
    cards.nth(1).locator(".runtime-access").nth(1).getByText("Ready"),
  ).toBeVisible();
});
test("blocked route shows scoped remediation and opens its Manager section", async ({
  page,
}) => {
  const report = reportWith(
    [
      runnableRoute("ws-1", "Alpine archive", "pi", false, [
        "workspace.agent_enabled",
      ]),
    ],
    [
      {
        id: "workspace.agent_enabled:ws-1",
        code: "workspace.agent_enabled",
        section: "workspaces",
        status: "action_required",
        summary: "Agent runs are disabled for this workspace.",
        remediation:
          "Enable agent runs for this workspace in the local manager.",
        workspace_id: "ws-1",
      },
      {
        id: "workspace.agent_enabled:ws-other",
        code: "workspace.agent_enabled",
        section: "workspaces",
        status: "action_required",
        summary: "Wrong workspace must never appear here.",
        remediation: "This is unrelated.",
        workspace_id: "ws-other",
      },
    ],
  );
  await mockApi(page, { diagnostics: report });
  await page.goto("./");
  await expect(
    page.getByText("Agent runs are disabled for this workspace."),
  ).toBeVisible();
  await expect(
    page.getByText("Wrong workspace must never appear here."),
  ).toHaveCount(0);
  await page
    .getByRole("button", { name: "Resolve", exact: true })
    .first()
    .click();
  await expect(page.getByRole("heading", { name: "Workspaces" })).toBeVisible();
});
test("diagnostics failure marks routes unavailable and leaves other pages usable", async ({
  page,
}) => {
  await mockApi(page);
  await page.goto("./");
  await expect(page.getByText("Default model: muse-spark")).toBeVisible();
  await page.route("**/api/diagnostics", async (route) => {
    await route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ error: "probe unavailable" }),
    });
  });
  await page.getByRole("button", { name: "Refresh" }).click();
  await expect(
    page.getByText("Current system observations are unavailable."),
  ).toBeVisible();
  await expect(
    page.getByText("stale and cannot authorize a start."),
  ).toBeVisible();
  await page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("button", { name: "Handoffs" })
    .click();
  await page.getByLabel("Workspace", { exact: true }).selectOption("ws-1");
  await expect(page.getByRole("button", { name: "Start run" })).toBeDisabled();
  await page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("button", { name: "Workspaces", exact: true })
    .click();
  await expect(page.getByText("Alpine archive").first()).toBeVisible();
});
test("Codex config binding remains separate from exact route readiness", async ({
  page,
}) => {
  const codexWorkspace = {
    ...workspace,
    runtime_grants: {
      ...workspace.runtime_grants,
      codex: {
        enabled: true,
        profile: null,
        security_binding: {
          source: "runtime-config",
          status: "ready",
          resolved_summary: {
            activePermissionProfile: "workspace-write",
            approvalPolicy: "on-request",
            approvalsReviewer: "user",
          },
        },
      },
    },
  };
  const report = reportWith([
    runnableRoute("ws-1", "Alpine archive", "pi", true),
    runnableRoute("ws-1", "Alpine archive", "codex", false, [
      "runtime.required_features",
    ]),
  ]);
  await mockApi(page, {
    diagnostics: report,
    workspaces: { workspaces: [codexWorkspace] },
  });
  await page.goto("./");
  await page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("button", { name: "Workspaces", exact: true })
    .click();
  const codex = page
    .locator(".runtime-access")
    .filter({ hasText: "Codex config" });
  await expect(
    codex.getByText("Security source: Codex config", { exact: true }),
  ).toBeVisible();
  await expect(codex.getByText("Blocked")).toBeVisible();
  await page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("button", { name: "Handoffs" })
    .click();
  await page.getByLabel("Workspace", { exact: true }).selectOption("ws-1");
  await page.getByLabel("Runtime / Route").selectOption("codex");
  await expect(page.getByRole("button", { name: "Start run" })).toBeDisabled();
});
test("ambiguous handoff start retry reuses its request ID", async ({
  page,
}) => {
  await mockApi(page);
  const requests: Array<Record<string, unknown>> = [];
  await page.route("**/api/workspaces/ws-1/jobs/job-1/runs", async (route) => {
    requests.push(route.request().postDataJSON());
    if (requests.length === 1) {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ error: "temporary response failure" }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ...run,
        run_id: "run-retried",
        model: "muse-spark",
      }),
    });
  });
  await page.goto("./");
  await page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("button", { name: "Handoffs" })
    .click();
  await page.getByLabel("Workspace", { exact: true }).selectOption("ws-1");
  await page.getByRole("button", { name: "Start run" }).click();
  await expect(page.getByText("temporary response failure")).toBeVisible();
  await page.getByRole("button", { name: "Start run" }).click();
  await expect(page.getByRole("heading", { name: "Agent runs" })).toBeVisible();
  expect(requests).toHaveLength(2);
  expect(requests[0]).toEqual(requests[1]);
  expect(Object.keys(requests[0]).sort()).toEqual(["request_id", "runtime"]);
});
test("overview and handoffs fit a mobile viewport without horizontal overflow", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockApi(page);
  await page.goto("./");
  const noHorizontalOverflow = async () =>
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
  await noHorizontalOverflow();
  await page.getByRole("button", { name: "Open navigation" }).click();
  await page
    .getByRole("navigation", { name: "Mobile navigation" })
    .getByRole("button", { name: "Handoffs" })
    .click();
  await expect(
    page.getByRole("navigation", { name: "Mobile navigation" }),
  ).toBeHidden();
  await page.getByLabel("Workspace", { exact: true }).selectOption("ws-1");
  await noHorizontalOverflow();
  await page.screenshot({
    path: "test-results/mobile-handoffs.png",
    fullPage: true,
  });
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
test("custom Pi profile builder creates, edits and deletes external access rules", async ({
  page,
}) => {
  await mockApi(page);
  const profileRows: Array<{
    id: string;
    revision: string;
    mutable: boolean;
    config: Record<string, unknown>;
  }> = [
    {
      id: "read-only",
      revision: "default-revision",
      mutable: false,
      config: piReadOnlyConfig,
    },
  ];
  const assignments: unknown[] = [];
  const saved: unknown[] = [];
  await page.route("**/api/runtimes/pi/profiles*", async (route) => {
    if (route.request().method() === "POST") {
      const body = route.request().postDataJSON();
      saved.push(body);
      const row = {
        id: body.id,
        revision: `revision-${saved.length}`,
        mutable: true,
        config: body.config,
      };
      const index = profileRows.findIndex((profile) => profile.id === row.id);
      if (index >= 0) profileRows[index] = row;
      else profileRows.push(row);
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(row),
      });
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ profiles: profileRows }),
    });
  });
  await page.route("**/api/runtimes/pi/profiles/*", async (route) => {
    const id = new URL(route.request().url()).pathname.split("/").pop();
    const index = profileRows.findIndex((profile) => profile.id === id);
    if (index >= 0) profileRows.splice(index, 1);
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ deleted: id }),
    });
  });
  await page.route("**/api/workspaces/ws-1/runtimes/pi", async (route) => {
    assignments.push(route.request().postDataJSON());
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: "{}",
    });
  });
  await page.goto("./");
  await page.getByRole("button", { name: "Profiles", exact: true }).click();
  await page.getByRole("button", { name: "Create from selected" }).click();
  await page.getByLabel("Profile ID").fill("reviewed-shared");
  await page.getByLabel("Enable edit and write").check();
  await page.getByLabel("Default file access").selectOption("deny");
  await page.getByRole("button", { name: "Add external root" }).click();
  await page
    .getByRole("textbox", { name: "External root 1", exact: true })
    .fill("/Volumes/shared");
  await page.getByLabel("Access for external root 1").selectOption("ask");
  await page.screenshot({
    path: "test-results/profile-editor-desktop.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: "test-results/profile-editor-mobile.png",
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.getByRole("button", { name: "Create profile" }).click();
  await expect.poll(() => saved.length).toBe(1);
  expect(
    (saved[0] as { config: typeof piReadOnlyConfig }).config.external_access
      .roots,
  ).toEqual([{ path: "/Volumes/shared", mode: "ask" }]);

  await page.getByRole("button", { name: "Workspaces", exact: true }).click();
  await page.getByRole("button", { name: "Change security" }).first().click();
  await page.getByRole("button", { name: /reviewed-shared/ }).click();
  await page.getByRole("button", { name: "Save security source" }).click();
  await expect.poll(() => assignments.length).toBe(1);
  expect(assignments[0]).toEqual({
    enabled: true,
    profile_id: "reviewed-shared",
    security_source: "profile",
  });

  await page.getByRole("button", { name: "Profiles", exact: true }).click();
  await page.getByRole("button", { name: /reviewed-shared/ }).click();
  await page.getByRole("button", { name: "Edit controls" }).click();
  await page.getByLabel("Shell commands").selectOption("ask");
  await page.getByRole("button", { name: "Save controls" }).click();
  await expect.poll(() => saved.length).toBe(2);
  expect((saved[1] as { expected_revision: string }).expected_revision).toBe(
    "revision-1",
  );

  await page.getByRole("button", { name: "Workspaces", exact: true }).click();
  await page.getByRole("button", { name: "Change security" }).first().click();
  await page.getByRole("button", { name: /read-only/ }).click();
  await page.getByRole("button", { name: "Save security source" }).click();
  await expect.poll(() => assignments.length).toBe(2);

  await page.getByRole("button", { name: "Profiles", exact: true }).click();
  await page.getByRole("button", { name: /reviewed-shared/ }).click();
  await page.getByRole("button", { name: "Delete", exact: true }).click();
  await page.getByRole("button", { name: "Delete profile" }).click();
  await expect
    .poll(() => profileRows.some((profile) => profile.id === "reviewed-shared"))
    .toBe(false);
});
test("Codex profile editor selects native permission profiles in workspace context", async ({
  page,
}) => {
  await mockApi(page);
  const codexConfig = {
    permissions: ":read-only",
    approvalPolicy: "on-request",
    approvalsReviewer: "user",
  };
  const profileRows = [
    {
      id: "read-only",
      revision: "native-rev",
      definitionRevision: "definition-rev",
      available: true,
      mutable: false,
      config: codexConfig,
    },
  ];
  const profileQueries: string[] = [];
  const saved: Array<{ id: string; config: Record<string, unknown> }> = [];
  await page.route("**/api/runtimes/codex/profiles*", async (route) => {
    const request = route.request();
    if (request.method() === "POST") {
      const body = request.postDataJSON();
      saved.push(body);
      profileRows.push({
        id: body.id,
        revision: "saved-rev",
        definitionRevision: "saved-definition-rev",
        available: true,
        mutable: true,
        config: body.config,
      });
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(profileRows.at(-1)),
      });
    }
    const url = new URL(request.url());
    profileQueries.push(url.searchParams.get("workspace_id") || "");
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        profiles: profileRows,
        permissionProfiles: [
          { id: ":read-only", description: "Read files", allowed: true },
          {
            id: "workspace-net",
            description: "Project network",
            allowed: true,
          },
        ],
      }),
    });
  });

  await page.goto("./");
  await page.getByRole("button", { name: "Profiles", exact: true }).click();
  await page.getByRole("button", { name: "Codex", exact: true }).click();
  await expect(page.getByRole("button", { name: "read-only" })).toBeVisible();
  expect(profileQueries.at(-1)).toBe("ws-1");
  await page.getByRole("button", { name: "Create from selected" }).click();
  await page.getByLabel("Profile ID").fill("workspace-network");
  await expect(page.getByLabel("Permission profile")).toBeVisible();
  await expect(page.getByLabel("Sandbox")).toHaveCount(0);
  await page.getByLabel("Permission profile").selectOption("workspace-net");
  const approvalOptions = page.getByLabel("Approvals").locator("option");
  await expect(approvalOptions).toHaveCount(2);
  expect(
    await approvalOptions.evaluateAll((options) =>
      options.map((option) => (option as HTMLOptionElement).value),
    ),
  ).toEqual(["on-request", "never"]);
  await page.getByLabel("Approvals").selectOption("on-request");
  await page
    .getByRole("button", { name: "Create profile", exact: true })
    .click();
  await expect.poll(() => saved.length).toBe(1);
  expect(saved[0]).toEqual({
    id: "workspace-network",
    config: {
      permissions: "workspace-net",
      approvalPolicy: "on-request",
      approvalsReviewer: "user",
    },
    expected_revision: null,
  });
  expect(saved[0].config).not.toHaveProperty("sandbox");
});

test("Codex workspace can follow current config with a live resolved summary", async ({
  page,
}) => {
  await mockApi(page);
  const selection: unknown[] = [];
  const runtimeConfig = {
    supported: true,
    available: true,
    status: "ready",
    revision: "opaque-security-revision",
    resolvedSummary: {
      activePermissionProfile: ":workspace",
      approvalPolicy: "on-request",
      approvalsReviewer: "user",
      provenance: "implicit/default",
    },
  };
  await page.route("**/api/runtimes/codex/profiles*", async (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        profiles: [
          {
            id: "read-only",
            revision: "profile-revision",
            mutable: false,
            available: true,
          },
        ],
        permissionProfiles: [],
        runtimeConfig,
      }),
    }),
  );
  await page.route("**/api/workspaces/ws-1/runtimes/codex", async (route) => {
    selection.push(route.request().postDataJSON());
    workspace.runtime_grants.codex.security_binding = {
      source: "runtime-config",
      revision: "old-observation",
      status: "ready",
      observed_revision: runtimeConfig.revision,
      resolved_summary: runtimeConfig.resolvedSummary,
    };
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: "{}",
    });
  });

  await page.goto("./");
  await page.getByRole("button", { name: "Workspaces", exact: true }).click();
  await page.getByRole("button", { name: "Change security" }).nth(1).click();
  const configMode = page.getByRole("radio", {
    name: /Use Codex config \(config.toml\)/,
  });
  await expect(configMode).toBeEnabled();
  await configMode.check();
  await expect(page.getByText(":workspace", { exact: true })).toBeVisible();
  await expect(
    page.getByText("implicit/default", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Save security source" }).click();
  await expect.poll(() => selection.length).toBe(1);
  expect(selection[0]).toEqual({
    enabled: false,
    security_source: "runtime-config",
    profile_id: null,
  });
  await expect(
    page.getByText("Use Codex config (config.toml)").first(),
  ).toBeVisible();
  await expect(page.getByText(/Following current Codex config/)).toBeVisible();
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
