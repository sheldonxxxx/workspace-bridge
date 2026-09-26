import { expect, test, type Page, type Route } from "@playwright/test";

const LOCAL_PI = "adapter_111111111111111111111111";
const GPU_PI = "adapter_222222222222222222222222";
const LOCAL_CODEX = "adapter_333333333333333333333333";
const REMOTE_PI = "adapter_666666666666666666666666";
const WS_ID = "ws_aaaaaaaaaaaaaaaaaaaaaaaa";
const JOB_ID = "job_bbbbbbbbbbbbbbbbbbbbbbbb";
const LOCAL_NODE = "node_aaaaaaaaaaaaaaaaaaaaaaaa";
const GPU_NODE = "node_bbbbbbbbbbbbbbbbbbbbbbbb";

type AdapterRow = {
  id: string;
  name: string;
  runtime_type: "pi" | "codex";
  base_url: string;
  enabled: boolean;
  revision: string;
  has_token: boolean;
  node_id: string;
  node_name: string;
  healthy: boolean;
  adapter_version: string;
  native_version: string;
  model_policy: { configured: boolean; enabled: string[]; default: string };
  savedToken: string;
};

function adapter(
  id: string,
  name: string,
  runtime_type: "pi" | "codex",
  base_url: string,
  model: string,
  savedToken: string,
  node_id = LOCAL_NODE,
  node_name = "Local Mac",
): AdapterRow {
  return {
    id,
    name,
    runtime_type,
    base_url,
    enabled: true,
    revision: `revision-${id.slice(-3)}`,
    has_token: true,
    node_id,
    node_name,
    healthy: true,
    adapter_version: "1.4.0",
    native_version: "2026.09",
    model_policy: { configured: true, enabled: [model], default: model },
    savedToken,
  };
}

const workspace = {
  id: WS_ID,
  name: "Alpine archive",
  root: "/Projects/alpine-archive",
  enabled: true,
  agent_enabled: true,
  write_scope: "handoff" as const,
  excludes: "[]",
  node_id: LOCAL_NODE,
  node_name: "Local Mac",
  routes: {
    [LOCAL_PI]: {
      adapter_id: LOCAL_PI,
      name: "Local Pi",
      runtime_type: "pi" as const,
      node_id: LOCAL_NODE,
      node_name: "Local Mac",
      adapter_enabled: true,
      enabled: true,
      is_default: true,
      security_binding: {
        source: "profile" as const,
        profile: { id: "read-only", revision: "local-profile-rev" },
      },
      profile: { id: "read-only", revision: "local-profile-rev" },
      effective_security: {
        source: "profile",
        profile_id: "read-only",
        bound_revision: "local-profile-rev",
        freshness: "current",
      },
    },
    [GPU_PI]: {
      adapter_id: GPU_PI,
      name: "GPU Pi",
      runtime_type: "pi" as const,
      node_id: LOCAL_NODE,
      node_name: "Local Mac",
      adapter_enabled: true,
      enabled: true,
      is_default: false,
      security_binding: {
        source: "profile" as const,
        profile: { id: "gpu-read-only", revision: "gpu-profile-rev" },
      },
      profile: { id: "gpu-read-only", revision: "gpu-profile-rev" },
      effective_security: {
        source: "profile",
        profile_id: "gpu-read-only",
        bound_revision: "gpu-profile-rev",
        freshness: "current",
      },
    },
    [LOCAL_CODEX]: {
      adapter_id: LOCAL_CODEX,
      name: "Local Codex",
      runtime_type: "codex" as const,
      node_id: LOCAL_NODE,
      node_name: "Local Mac",
      adapter_enabled: true,
      enabled: true,
      is_default: false,
      security_binding: {
        source: "runtime-config" as const,
        revision: "codex-config-rev",
        status: "ready",
        resolved_summary: {
          activePermissionProfile: ":read-only",
          approvalPolicy: "on-request",
          approvalsReviewer: "user",
        },
      },
      profile: null,
      effective_security: {
        source: "runtime-config",
        bound_revision: "codex-config-rev",
        freshness: "current",
        status: "ready",
        resolved_summary: {
          activePermissionProfile: ":read-only",
          approvalPolicy: "on-request",
          approvalsReviewer: "user",
        },
      },
    },
  },
};

const handoff = {
  id: JOB_ID,
  title: "Review project structure",
  state: "prepared",
  path: "/Projects/alpine-archive/.workspace-handoff/job-b",
  copy_prompt: "Review the structure",
};

const routeReport = {
  generated_at: "2026-09-25T08:00:00Z",
  mode: "live",
  overall: {
    status: "unknown",
    summary: "Some adapter freshness was not observed.",
    counts: { pass: 10, warning: 0, unknown: 1, action_required: 0, failed: 0 },
  },
  checks: [
    {
      id: `model.default_freshness:${WS_ID}:${LOCAL_CODEX}`,
      code: "model.default_freshness",
      section: "models_profiles",
      status: "action_required",
      summary: "The Local Codex default model is unavailable.",
      remediation: "Choose a currently discovered model for Local Codex.",
      workspace_id: WS_ID,
      adapter_id: LOCAL_CODEX,
      runtime_type: "codex",
    },
  ],
  runnable_routes: [
    {
      id: `${WS_ID}:${LOCAL_PI}`,
      workspace_id: WS_ID,
      workspace_name: workspace.name,
      adapter_id: LOCAL_PI,
      adapter_name: "Local Pi",
      node_id: LOCAL_NODE,
      node_name: "Local Mac",
      runtime_type: "pi",
      ready: true,
      is_default: true,
      status: "ready",
      summary: "This exact workspace and adapter route is ready.",
      blockers: [],
      security_source: "profile",
      profile: { id: "read-only", revision: "local-profile-rev" },
      default_model_selector: "pi-local-fast",
    },
    {
      id: `${WS_ID}:${GPU_PI}`,
      workspace_id: WS_ID,
      workspace_name: workspace.name,
      adapter_id: GPU_PI,
      adapter_name: "GPU Pi",
      node_id: LOCAL_NODE,
      node_name: "Local Mac",
      runtime_type: "pi",
      ready: true,
      is_default: false,
      status: "ready",
      summary: "This exact workspace and adapter route is ready.",
      blockers: [],
      security_source: "profile",
      profile: { id: "gpu-read-only", revision: "gpu-profile-rev" },
      default_model_selector: "pi-gpu-large",
    },
    {
      id: `${WS_ID}:${LOCAL_CODEX}`,
      workspace_id: WS_ID,
      workspace_name: workspace.name,
      adapter_id: LOCAL_CODEX,
      adapter_name: "Local Codex",
      node_id: LOCAL_NODE,
      node_name: "Local Mac",
      runtime_type: "codex",
      ready: false,
      is_default: false,
      status: "blocked",
      summary: "Route has one blocker.",
      blockers: ["model.default_freshness"],
      security_source: "runtime-config",
      profile: null,
      default_model_selector: "codex-5",
    },
  ],
};

function publicAdapter(row: AdapterRow) {
  const { savedToken: _savedToken, ...publicRow } = row;
  return publicRow;
}

function profileCatalog(id: string) {
  const profileId = id === GPU_PI ? "gpu-read-only" : "read-only";
  const permission = id === LOCAL_CODEX ? ":read-only" : undefined;
  return {
    adapter_id: id,
    profiles: [
      {
        id: profileId,
        revision: `profile-rev-${id.slice(-3)}`,
        mutable: false,
        available: true,
        config: permission
          ? {
              permissions: permission,
              approvalPolicy: "on-request",
              approvalsReviewer: "user",
            }
          : {
              external_access: { default_mode: "deny", roots: [] },
              protected_patterns: [".git/**", ".env*"],
            },
      },
    ],
    permissionProfiles: permission
      ? [{ id: permission, allowed: true, description: "Read only" }]
      : [],
    ...(id === LOCAL_CODEX
      ? {
          runtimeConfig: {
            supported: true,
            available: true,
            status: "ready",
            revision: "codex-config-rev",
            resolvedSummary: {
              activePermissionProfile: ":read-only",
              approvalPolicy: "on-request",
              approvalsReviewer: "user",
              provenance: "native",
            },
          },
        }
      : {}),
  };
}

async function mockApi(
  page: Page,
  options: {
    zeroRoute?: boolean;
    blockedDefault?: boolean;
    runSnapshot?: boolean;
  } = {},
) {
  const adapters = [
    adapter(
      LOCAL_PI,
      "Local Pi",
      "pi",
      "http://host.docker.internal:8780",
      "pi-local-fast",
      "local-pi-private-token",
    ),
    adapter(
      GPU_PI,
      "GPU Pi",
      "pi",
      "http://gpu-host:8780",
      "pi-gpu-large",
      "gpu-pi-private-token",
    ),
    adapter(
      LOCAL_CODEX,
      "Local Codex",
      "codex",
      "http://host.docker.internal:8772",
      "codex-5",
      "codex-private-token",
    ),
  ];
  if (options.zeroRoute) {
    adapters.push(
      adapter(
        REMOTE_PI,
        "GPU-only Pi",
        "pi",
        "http://gpu-only:8780",
        "pi-gpu-only",
        "gpu-only-private-token",
        GPU_NODE,
        "GPU Server",
      ),
    );
  }
  type WorkspacePayload = typeof workspace & {
    available_adapters?: Array<{
      adapter_id: string;
      name: string;
      runtime_type: "pi" | "codex";
      enabled: boolean;
      node_id: string;
      route_enabled?: boolean;
      readiness?: string;
    }>;
  };
  const workspacePayload = JSON.parse(
    JSON.stringify(workspace),
  ) as WorkspacePayload;
  const diagnosticsPayload = JSON.parse(
    JSON.stringify(routeReport),
  ) as typeof routeReport;
  if (options.zeroRoute) {
    workspacePayload.routes = {};
    workspacePayload.available_adapters = [
      {
        adapter_id: LOCAL_CODEX,
        name: "Local Codex",
        runtime_type: "codex",
        enabled: true,
        node_id: LOCAL_NODE,
        route_enabled: false,
        readiness: "unbound",
      },
    ];
    workspacePayload.node_adapter_count = 1;
    diagnosticsPayload.runnable_routes = [];
  }
  if (options.blockedDefault) {
    const localDefault = diagnosticsPayload.runnable_routes.find(
      (route) => route.adapter_id === LOCAL_PI,
    );
    if (localDefault) {
      localDefault.ready = false;
      localDefault.status = "blocked";
      localDefault.summary = "The workspace default route is blocked.";
      localDefault.blockers = ["route.security_freshness"];
      localDefault.is_default = true;
    }
  }
  const snapshotRun = {
    run_id: "run-snapshot",
    adapter_id: LOCAL_PI,
    adapter_name: "Local Pi",
    node_id: LOCAL_NODE,
    node_name: "Local Mac",
    node_revision: "node-run-rev",
    adapter_revision: "adapter-run-rev",
    runtime_type: "pi",
    phase: "terminal",
    outcome: "succeeded",
    workspace_id: WS_ID,
    workspace_name: workspace.name,
    job_id: JOB_ID,
    handoff_title: handoff.title,
    model: "pi-local-fast",
    conversation_id: "conversation-snapshot",
    created: "2026-09-25T08:10:00Z",
    updated: "2026-09-25T08:12:00Z",
    effective_security: {
      source: "profile",
      profile_id: "read-only",
      bound_revision: "profile-run-rev",
      effective_revision: "profile-run-rev",
    },
    token_usage: {
      input_tokens: 120,
      cached_input_tokens: 20,
      cache_write_input_tokens: 1,
      output_tokens: 30,
      reasoning_output_tokens: 4,
      total_tokens: 150,
    },
  };
  const nodes = [
    {
      id: LOCAL_NODE,
      name: "Local Mac",
      base_url: "http://127.0.0.1:8770",
      enabled: true,
      revision: "node-local-rev",
      has_token: true,
      health: "healthy",
      protocol: 1,
      node_version: "2026.09",
      capabilities: ["workspace", "runtime"],
      allowed_root_count: 1,
    },
    {
      id: GPU_NODE,
      name: "GPU Server",
      base_url: "http://gpu-node:8770",
      enabled: true,
      revision: "node-gpu-rev",
      has_token: true,
      health: "healthy",
      protocol: 1,
      node_version: "2026.09",
      capabilities: ["workspace", "runtime"],
      allowed_root_count: 1,
    },
  ];
  const calls: Array<{
    path: string;
    method: string;
    body?: Record<string, unknown>;
  }> = [];
  const starts: Array<Record<string, unknown>> = [];
  const tests: Array<Record<string, unknown>> = [];
  const updates: Array<{ id: string; body: Record<string, unknown> }> = [];
  const respond = async (route: Route, body: unknown, status = 200) =>
    route.fulfill({
      status,
      contentType: "application/json",
      body: JSON.stringify(body),
    });

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();
    const body =
      method === "GET"
        ? undefined
        : (request.postDataJSON() as Record<string, unknown>);
    calls.push({ path, method, body });

    if (path === "/api/status") {
      await respond(route, {
        version: "0.1.0",
        listen_mode: "loopback",
        mcp_port: 8765,
        bridge: { configured: true, enabled: true },
        nodes,
        adapters: { adapters: adapters.map(publicAdapter) },
      });
      return;
    }
    if (path === "/api/diagnostics") {
      await respond(route, diagnosticsPayload);
      return;
    }
    if (path === "/api/workspaces") {
      await respond(route, { workspaces: [workspacePayload] });
      return;
    }
    if (path === "/api/events") {
      await respond(route, { events: [] });
      return;
    }
    if (path === "/api/runs" && method === "GET") {
      await respond(route, {
        runs: options.runSnapshot ? [snapshotRun] : [],
        next_offset: null,
      });
      return;
    }
    if (path === `/api/runs/${snapshotRun.run_id}` && method === "GET") {
      await respond(route, snapshotRun);
      return;
    }
    if (
      path.startsWith(`/api/runs/${snapshotRun.run_id}/activities`) ||
      path.startsWith(`/api/runs/${snapshotRun.run_id}/executions`)
    ) {
      await respond(route, {
        activities: [],
        executions: [],
        next_cursor: null,
      });
      return;
    }
    if (path === `/api/workspaces/${WS_ID}/jobs`) {
      await respond(route, { handoffs: [handoff] });
      return;
    }
    if (path === `/api/workspaces/${WS_ID}/runs`) {
      await respond(route, {
        runs: options.runSnapshot ? [snapshotRun] : [],
      });
      return;
    }
    if (path === `/api/workspaces/${WS_ID}/document`) {
      await respond(route, {
        content:
          "Handoff summary: inspect the project and run the agreed checks.",
      });
      return;
    }
    if (path === "/api/nodes/test" && method === "POST") {
      await respond(route, { success: true });
      return;
    }
    if (path === "/api/nodes" && method === "POST") {
      const created = {
        ...nodes[0],
        id: "node_cccccccccccccccccccccccc",
        name: String(body?.name || "New Node"),
        base_url: String(body?.base_url || "http://new-node:8770"),
      };
      nodes.push(created);
      await respond(route, created, 201);
      return;
    }
    const nodeMatch = path.match(/^\/api\/nodes\/(node_[0-9a-f]{24})$/);
    if (nodeMatch && method === "PATCH") {
      const node = nodes.find((item) => item.id === nodeMatch[1]);
      if (node) Object.assign(node, body || {});
      await respond(route, node || { ok: true });
      return;
    }
    if (path === "/api/adapters" && method === "GET") {
      await respond(route, { adapters: adapters.map(publicAdapter) });
      return;
    }
    if (path === "/api/adapters" && method === "POST") {
      const created = adapter(
        "adapter_444444444444444444444444",
        String(body?.name),
        body?.runtime_type === "codex" ? "codex" : "pi",
        String(body?.base_url),
        "new-default",
        String(body?.token),
      );
      adapters.push(created);
      await respond(route, publicAdapter(created), 201);
      return;
    }
    if (path === "/api/adapters/test" && method === "POST") {
      tests.push(body || {});
      await respond(route, {
        success: true,
        adapter_id: body?.adapter_id,
        runtime_type: body?.runtime_type,
        native_runtime: body?.runtime_type,
        native_version: "2026.09",
        adapter_version: "1.4.0",
      });
      return;
    }
    const adapterMatch = path.match(
      /^\/api\/adapters\/(adapter_[0-9a-f]{24})(?:\/(.*))?$/,
    );
    if (adapterMatch) {
      const [, id, leaf] = adapterMatch;
      const row = adapters.find((item) => item.id === id);
      if (!row) {
        await respond(route, { error: "Unknown adapter" }, 404);
        return;
      }
      if (!leaf && method === "PATCH") {
        updates.push({ id, body: body || {} });
        Object.assign(row, body);
        if (body?.token) row.savedToken = String(body.token);
        await respond(route, publicAdapter(row));
        return;
      }
      if (!leaf && method === "GET") {
        await respond(route, publicAdapter(row));
        return;
      }
      if (leaf === "models" && method === "GET") {
        const model =
          id === LOCAL_PI
            ? {
                selector: "pi-local-fast",
                displayName: "Local Pi model",
                reasoningOptions: ["low", "high"],
              }
            : id === GPU_PI
              ? {
                  selector: "pi-gpu-large",
                  displayName: "GPU Pi model",
                  reasoningOptions: ["low", "high"],
                }
              : {
                  selector: "codex-5",
                  displayName: "Codex model",
                  reasoningOptions: ["low", "high"],
                };
        await respond(route, {
          adapter_id: id,
          models: [model],
          policy: row.model_policy,
        });
        return;
      }
      if (leaf === "profiles" && method === "GET") {
        await respond(route, profileCatalog(id));
        return;
      }
    }
    if (
      path.startsWith(`/api/workspaces/${WS_ID}/jobs/`) &&
      path.endsWith("/runs") &&
      method === "POST"
    ) {
      starts.push(body || {});
      const selected = adapters.find((item) => item.id === body?.adapter_id);
      await respond(route, {
        run_id: `run-${starts.length}`,
        conversation_id: `conversation-${starts.length}`,
        adapter_id: selected?.id,
        adapter_name: selected?.name,
        runtime_type: selected?.runtime_type,
        phase: "active",
        active_state: "running",
        model: selected?.model_policy.default,
      });
      return;
    }
    if (
      path.startsWith(`/api/workspaces/${WS_ID}/routes/`) &&
      method === "POST"
    ) {
      await respond(route, { ok: true });
      return;
    }
    await respond(route, { ok: true });
  });
  return { calls, starts, tests, updates, adapters };
}

async function navigate(page: Page, title: string) {
  await page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("button", { name: title, exact: true })
    .click();
}

test("same-runtime adapter instances stay distinct in workspaces and exact handoff start", async ({
  page,
}) => {
  const api = await mockApi(page);
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("./");
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
  await navigate(page, "Workspaces");
  await expect(page.getByText("Local Pi · Pi", { exact: true })).toBeVisible();
  await expect(page.getByText("GPU Pi · Pi", { exact: true })).toBeVisible();
  await expect(
    page.getByText("Local Codex · Codex", { exact: true }),
  ).toBeVisible();

  await navigate(page, "Handoffs");
  await page.getByLabel("Workspace", { exact: true }).selectOption(WS_ID);
  await expect(page.getByText("Default model: pi-local-fast")).toBeVisible();
  const target = page.getByLabel("Execution target", { exact: true });
  await target.selectOption(LOCAL_CODEX);
  await expect(
    page.getByText("The Local Codex default model is unavailable."),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Start run" })).toBeDisabled();
  await target.selectOption(GPU_PI);
  await expect(page.getByRole("button", { name: "Start run" })).toBeEnabled();
  await page.getByRole("button", { name: "Start run" }).click();
  await expect(page.getByRole("heading", { name: "Agent runs" })).toBeVisible();
  expect(api.starts).toHaveLength(1);
  expect(api.starts[0]?.adapter_id).toBe(GPU_PI);
  expect("runtime" in api.starts[0]).toBe(false);
  expect(Object.keys(api.starts[0] || {}).sort()).toEqual([
    "adapter_id",
    "request_id",
  ]);
  expect(errors).toEqual([]);
});

test("adapter add/edit forms test current values and never read a token back", async ({
  page,
}) => {
  const api = await mockApi(page);
  await page.goto("./");
  await navigate(page, "Adapters");

  await page.getByRole("button", { name: "Add adapter" }).click();
  await page.getByLabel("Name", { exact: true }).fill("Backup Pi");
  await page
    .getByLabel("Base URL", { exact: true })
    .fill("http://gpu-backup:8780");
  await page
    .getByLabel("Adapter token", { exact: true })
    .fill("new-private-token");
  await page.getByRole("button", { name: "Test connection" }).click();
  await expect(
    page.getByRole("status").filter({ hasText: "Connected: pi" }),
  ).toBeVisible();
  expect(api.tests.at(-1)).toMatchObject({
    name: "Backup Pi",
    token: "new-private-token",
  });
  await page
    .getByRole("button", { name: "Add adapter", exact: true })
    .last()
    .click();
  await expect(
    page.getByRole("heading", { name: "Backup Pi · Pi" }),
  ).toBeVisible();
  await expect(page.getByText("new-private-token")).toHaveCount(0);

  const gpuRow = page
    .locator(".adapter-card")
    .filter({ hasText: "GPU Pi · Pi" });
  await gpuRow.getByRole("button", { name: "Edit" }).click();
  const tokenField = page.getByLabel("Adapter token", { exact: true });
  await expect(tokenField).toHaveValue("");
  await page
    .getByLabel("Base URL", { exact: true })
    .fill("http://gpu-updated:8780");
  await page.getByRole("button", { name: "Test connection" }).click();
  expect(api.tests.at(-1)).toMatchObject({
    adapter_id: GPU_PI,
    base_url: "http://gpu-updated:8780",
    token: "",
  });
  await page.getByRole("button", { name: "Save adapter" }).click();
  await expect(
    gpuRow.getByText("http://gpu-updated:8780").first(),
  ).toBeVisible();
  expect(api.updates.at(-1)).toEqual({
    id: GPU_PI,
    body: {
      name: "GPU Pi",
      base_url: "http://gpu-updated:8780",
      token: "",
      enabled: true,
    },
  });
  expect(api.adapters.find((item) => item.id === GPU_PI)?.savedToken).toBe(
    "gpu-pi-private-token",
  );

  await gpuRow.getByRole("button", { name: "Edit" }).click();
  await expect(page.getByLabel("Adapter token", { exact: true })).toHaveValue(
    "",
  );
  await expect(page.getByText("gpu-pi-private-token")).toHaveCount(0);
});

test("model and profile discovery use each adapter ID", async ({ page }) => {
  const api = await mockApi(page);
  await page.goto("./");
  await navigate(page, "Adapters");
  const gpuRow = page
    .locator(".adapter-card")
    .filter({ hasText: "GPU Pi · Pi" });
  await gpuRow.getByRole("button", { name: "Models" }).click();
  await expect(
    page.getByRole("heading", { name: "GPU Pi models" }),
  ).toBeVisible();
  await expect(
    page.getByRole("checkbox", { name: /GPU Pi model pi-gpu-large/ }),
  ).toBeVisible();
  expect(
    api.calls.some((call) => call.path === `/api/adapters/${GPU_PI}/models`),
  ).toBe(true);
  await page.getByRole("button", { name: "Cancel" }).click();

  const profileTabs = page.getByRole("group", { name: "Adapter" });
  await profileTabs.getByRole("button", { name: "GPU Pi · Pi" }).click();
  await expect(
    page.getByRole("heading", { name: "gpu-read-only" }),
  ).toBeVisible();
  expect(
    api.calls.some((call) => call.path === `/api/adapters/${GPU_PI}/profiles`),
  ).toBe(true);
  expect(
    api.calls.some(
      (call) => call.path === `/api/adapters/${LOCAL_PI}/profiles`,
    ),
  ).toBe(true);
});

test("manager pages fit desktop and mobile viewports", async ({ page }) => {
  await mockApi(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("./");
  const noOverflow = async () =>
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
  await noOverflow();
  await navigate(page, "Workspaces");
  await noOverflow();

  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "Open navigation" }).click();
  await page
    .getByRole("navigation", { name: "Mobile navigation" })
    .getByRole("button", { name: "Handoffs" })
    .click();
  await page.getByLabel("Workspace", { exact: true }).selectOption(WS_ID);
  await noOverflow();
  await page.getByRole("button", { name: "Open navigation" }).click();
  await page
    .getByRole("navigation", { name: "Mobile navigation" })
    .getByRole("button", { name: "Adapters" })
    .click();
  await expect(
    page.getByRole("heading", { name: "Adapters", level: 1 }),
  ).toBeVisible();
  await noOverflow();
  await page.getByRole("button", { name: "Open navigation" }).click();
  await page
    .getByRole("navigation", { name: "Mobile navigation" })
    .getByRole("button", { name: "Nodes" })
    .click();
  await expect(
    page.getByRole("heading", { name: "Nodes", level: 1 }),
  ).toBeVisible();
  await noOverflow();
});

test("Nodes show authority status and keep Node and adapter tokens write-only", async ({
  page,
}) => {
  const api = await mockApi(page);
  await page.goto("./");
  await navigate(page, "Nodes");
  await expect(
    page.getByRole("heading", { name: "Nodes", level: 1 }),
  ).toBeVisible();
  await expect(page.getByRole("heading", { name: "Local Mac" })).toBeVisible();
  await expect(page.getByText("Node Protocol", { exact: true })).toHaveCount(2);
  await expect(page.getByText("Local Pi · Pi", { exact: true })).toBeVisible();

  await page
    .getByRole("button", { name: "Add Node", exact: true })
    .first()
    .click();
  await page.getByLabel("Name", { exact: true }).fill("Review Node");
  await page
    .getByLabel("Node URL", { exact: true })
    .fill("http://review-node:8770");
  await page
    .getByLabel("Node token", { exact: true })
    .fill("review-node-secret");
  await page.getByRole("button", { name: "Test connection" }).click();
  await expect(page.getByRole("status")).toHaveText(
    "Connected: Node Protocol is ready.",
  );
  expect(api.calls.at(-1)).toMatchObject({
    path: "/api/nodes/test",
    method: "POST",
    body: { token: "review-node-secret", base_url: "http://review-node:8770" },
  });
  await page
    .getByRole("button", { name: "Add Node", exact: true })
    .last()
    .click();
  await expect(page.getByText("review-node-secret")).toHaveCount(0);
  expect(
    api.calls.some(
      (call) => call.path === "/api/nodes" && call.method === "POST",
    ),
  ).toBe(true);

  const localNode = page.locator(".node-card").filter({ hasText: "Local Mac" });
  await localNode.getByRole("button", { name: "Edit" }).first().click();
  await expect(page.getByLabel("Node token", { exact: true })).toHaveValue("");
  await expect(page.getByText("node-test-token")).toHaveCount(0);
  await page.getByRole("button", { name: "Cancel" }).click();
  await localNode.getByRole("button", { name: "Add adapter" }).click();
  await expect(
    page.getByText(/owning Node's private SQLite state/),
  ).toBeVisible();
  await expect(page.getByText(/Manager never reads it back/)).toBeVisible();
});

test("zero-route workspaces offer only same-Node targets and no implicit fallback", async ({
  page,
}) => {
  const api = await mockApi(page, { zeroRoute: true });
  await page.goto("./");
  await navigate(page, "Workspaces");
  const card = page
    .locator(".workspace-entry")
    .filter({ hasText: workspace.name });
  await expect(card.getByText("No execution targets configured")).toBeVisible();
  const addTarget = card.getByLabel(
    `Add execution target for ${workspace.name}`,
  );
  await expect(
    addTarget.getByRole("option", { name: /Local Codex/ }),
  ).toHaveCount(1);
  await expect(
    addTarget.getByRole("option", { name: /GPU-only Pi/ }),
  ).toHaveCount(0);
  await addTarget.selectOption(LOCAL_CODEX);
  await card.getByRole("button", { name: "Add target" }).click();
  expect(
    api.calls.find(
      (call) => call.path === `/api/workspaces/${WS_ID}/routes/${LOCAL_CODEX}`,
    ),
  ).toMatchObject({ method: "POST", body: { enabled: false } });

  await navigate(page, "Handoffs");
  await page.getByLabel("Workspace", { exact: true }).selectOption(WS_ID);
  const route = page.getByLabel("Execution target", { exact: true });
  await expect(route).toBeDisabled();
  await expect(
    route.getByRole("option", { name: "No canonical route reported" }),
  ).toHaveCount(1);
  await expect(page.getByRole("button", { name: "Start run" })).toBeDisabled();
});

test("handoff summaries, blocked defaults, and run security snapshots stay explicit", async ({
  page,
}) => {
  const api = await mockApi(page, { blockedDefault: true, runSnapshot: true });
  await page.goto("./");
  await navigate(page, "Workspaces");
  await expect(page.getByText("Route readiness").first()).toBeVisible();
  await expect(
    page.getByText(/Profile read-only · revision local-profil/).first(),
  ).toBeVisible();
  await expect(
    page.getByText(
      /Codex runtime config · :read-only · approval on-request · reviewer user/,
    ),
  ).toBeVisible();
  const workspaceCard = page
    .locator(".workspace-entry")
    .filter({ hasText: workspace.name });
  await workspaceCard.getByRole("button", { name: "Clear default" }).click();
  await expect
    .poll(() =>
      api.calls.some(
        (call) =>
          call.path === `/api/workspaces/${WS_ID}/routes/default` &&
          call.method === "DELETE",
      ),
    )
    .toBe(true);
  const gpuRoute = workspaceCard
    .locator(".target-row")
    .filter({ hasText: "GPU Pi · Pi" });
  await gpuRoute.getByRole("button", { name: "Set as default" }).click();
  await expect
    .poll(() =>
      api.calls.some(
        (call) =>
          call.path === `/api/workspaces/${WS_ID}/routes/default` &&
          call.method === "POST" &&
          call.body?.adapter_id === GPU_PI,
      ),
    )
    .toBe(true);
  await navigate(page, "Handoffs");
  await page.getByLabel("Workspace", { exact: true }).selectOption(WS_ID);
  await expect(
    page.getByLabel("Execution target", { exact: true }),
  ).toHaveValue(LOCAL_PI);
  await expect(
    page.getByText(/Security: Profile read-only/).first(),
  ).toBeVisible();
  await expect(page.getByText(/Node: Local Mac/).first()).toBeVisible();
  await expect(
    page.getByText(/Default model: pi-local-fast/).first(),
  ).toBeVisible();
  const routeSelector = page.getByLabel("Execution target", { exact: true });
  await routeSelector.selectOption(LOCAL_CODEX);
  await expect(
    page.getByText(
      /Security: Codex runtime config · :read-only · approval on-request · reviewer user/,
    ),
  ).toBeVisible();
  await routeSelector.selectOption(LOCAL_PI);
  await expect(
    page.getByText("Blocked by route.security_freshness."),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Start run" })).toBeDisabled();
  await page.getByRole("button", { name: "TASK" }).click();
  await expect(
    page.getByText("Handoff summary: inspect the project"),
  ).toBeVisible();
  await page.getByRole("button", { name: "Close" }).last().click();

  await navigate(page, "Runs");
  await expect(page.getByRole("heading", { name: "Agent runs" })).toBeVisible();
  await page.getByRole("button", { name: "Open run" }).click();
  await expect(page.getByText("Security used for this run")).toBeVisible();
  await expect(
    page.getByText(/Profile read-only · bound revision profile-run-rev/),
  ).toBeVisible();
  await expect(page.getByText(/Local Mac · rev node-run-rev/)).toBeVisible();
  await expect(page.getByText("Token usage")).toBeVisible();
  await expect(page.getByText("150", { exact: true }).first()).toBeVisible();
});
