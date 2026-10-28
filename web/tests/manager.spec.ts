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
  runtime_type: "pi" | "codex" | "claude" | "opencode";
  enabled: boolean;
  revision: string;
  node_id: string;
  node_name: string;
  healthy: boolean;
  adapter_version: string;
  native_version: string;
  model_policy: { configured: boolean; enabled: string[]; default: string };
  features?: Record<string, number>;
  security: { enforces: string[]; native: boolean };
  projects_root: string;
  settings: Record<string, string>;
};

const FULL_ENFORCEMENT = [
  "read_inside",
  "read_outside",
  "write_inside",
  "write_outside",
  "shell",
  "web",
  "extensions",
  "protected",
  "external_roots",
  "commands",
];
const CODEX_ENFORCEMENT = [
  "write_inside",
  "write_outside",
  "protected",
  "commands",
];

function adapter(
  id: string,
  name: string,
  runtime_type: "pi" | "codex" | "claude" | "opencode",
  model: string,
  node_id = LOCAL_NODE,
  node_name = "Local Mac",
): AdapterRow {
  return {
    id,
    name,
    runtime_type,
    enabled: true,
    revision: `revision-${id.slice(-3)}`,
    node_id,
    node_name,
    healthy: true,
    adapter_version: "1.4.0",
    native_version: "2026.09",
    model_policy: { configured: true, enabled: [model], default: model },
    security:
      runtime_type === "codex"
        ? { enforces: CODEX_ENFORCEMENT, native: true }
        : { enforces: FULL_ENFORCEMENT, native: false },
    projects_root: "/Projects",
    settings: {},
  };
}

const workspace = {
  id: WS_ID,
  name: "Alpine archive",
  root: "/Projects/alpine-archive",
  enabled: true,
  write_scope: "read-only" as const,
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
        source: "native" as const,
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
        source: "native",
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
      security_source: "native",
      profile: null,
      default_model_selector: "codex-5",
    },
  ],
};

function publicAdapter(row: AdapterRow) {
  const { projects_root: _root, settings: _settings, ...publicRow } = row;
  return publicRow;
}

async function mockApi(
  page: Page,
  options: {
    zeroRoute?: boolean;
    remoteWorkspace?: boolean;
    remoteWorkspaceEnabled?: boolean;
    remoteAdapter?: boolean;
    blockedDefault?: boolean;
    runSnapshot?: boolean;
    activeRun?: boolean;
    pendingRequest?: boolean;
    codexQuota?:
      "ok" | "empty" | "error" | "ordinary-unavailable" | "same-duration";
    codexQuotaDelayMs?: number;
  } = {},
) {
  const adapters = [
    adapter(LOCAL_PI, "Local Pi", "pi", "pi-local-fast"),
    adapter(GPU_PI, "GPU Pi", "pi", "pi-gpu-large"),
    adapter(LOCAL_CODEX, "Local Codex", "codex", "codex-5"),
  ];
  if (options.zeroRoute || options.remoteAdapter) {
    adapters.push(
      adapter(
        REMOTE_PI,
        "GPU-only Pi",
        "pi",
        "pi-gpu-only",
        GPU_NODE,
        "GPU Server",
      ),
    );
  }
  const codexRow = adapters.find((item) => item.id === LOCAL_CODEX);
  if (codexRow) codexRow.features = { usageLimits: 1 };
  type WorkspacePayload = typeof workspace & {
    available_adapters?: Array<{
      adapter_id: string;
      name: string;
      runtime_type: "pi" | "codex" | "claude" | "opencode";
      enabled: boolean;
      node_id: string;
      route_enabled?: boolean;
      readiness?: string;
    }>;
  };
  const workspacePayload = JSON.parse(
    JSON.stringify(workspace),
  ) as WorkspacePayload;
  const workspacePayloads = [workspacePayload];
  if (options.remoteWorkspace) {
    workspacePayloads.push({
      ...JSON.parse(JSON.stringify(workspacePayload)),
      id: "ws_cccccccccccccccccccccccc",
      name: "GPU archive",
      root: "/Projects/gpu-archive",
      node_id: GPU_NODE,
      node_name: "GPU Server",
      enabled: options.remoteWorkspaceEnabled ?? true,
      routes: {},
    });
  }
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
    phase: options.activeRun ? "active" : "terminal",
    outcome: options.activeRun ? null : "succeeded",
    input_capabilities: {
      steer: Boolean(options.activeRun),
      queue: Boolean(options.activeRun),
    },
    input_messages: options.activeRun
      ? [
          { requestId: "chatgpt-queue", mode: "queue", status: "queued" },
          { requestId: "chatgpt-steer", mode: "steer", status: "delivered" },
        ]
      : [],
    interactions: options.pendingRequest
      ? [
          {
            id: "int_pending",
            kind: "choice",
            state: "pending",
            details: {
              title: "Run npm test",
              resource: "npm test",
              choices: [
                { id: "allow", label: "Allow once", semantic: "approve" },
                { id: "deny", label: "Deny", semantic: "deny" },
              ],
            },
          },
        ]
      : [],
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
      enabled: true,
      revision: "node-local-rev",
      transport: "local",
      health: "healthy",
      protocol: 2,
      node_version: "2026.09",
      capabilities: ["workspace", "runtime"],
      allowed_root_count: 1,
    },
    {
      id: GPU_NODE,
      name: "GPU Server",
      enabled: true,
      revision: "node-gpu-rev",
      transport: "paired",
      connection: {
        connected_at: "2026-09-25T08:00:00Z",
        last_seen: "2026-09-25T08:05:00Z",
        disconnected_at: null,
      },
      health: "healthy",
      protocol: 2,
      node_version: "2026.09",
      capabilities: ["workspace", "runtime"],
      allowed_root_count: 1,
    },
  ];
  const pairings: Array<Record<string, unknown>> = [];
  const plugins = [
    {
      id: "pi",
      runtime_type: "pi" as const,
      version: "1.0.0",
      description: "Pi through the Pi coding agent SDK",
      settings: ["pi_binary", "agent_dir"],
      features: ["steer", "queue"],
    },
    {
      id: "opencode",
      runtime_type: "opencode" as const,
      version: "1.0.0",
      description: "OpenCode through opencode serve",
      settings: ["binary"],
      features: ["steer", "queue"],
    },
    {
      id: "codex",
      runtime_type: "codex" as const,
      version: "1.0.0",
      description: "Codex through codex app-server",
      settings: [],
      features: ["steer", "queue"],
    },
  ];
  const baseRules = {
    read_inside: "allow",
    read_outside: "deny",
    write_inside: "deny",
    write_outside: "deny",
    shell: "deny",
    web: "deny",
    extensions: "deny",
    protected: [] as string[],
    external_roots: [] as unknown[],
    commands: [] as unknown[],
  };
  const bridgeProfiles: Array<Record<string, unknown>> = [
    {
      id: "read-only",
      revision: "profile-rev-readonly",
      builtin: true,
      description: "Reads the workspace; no edits, shell, web or extensions.",
      rules: baseRules,
      restricts: ["write_inside", "write_outside"],
      routes: [
        {
          workspace_id: "ws_aaaaaaaaaaaaaaaaaaaaaaaa",
          workspace_name: "Alpha",
          adapter_id: "adapter_codex",
          adapter_name: "Local Codex",
          enabled: true,
        },
      ],
    },
    {
      id: "reviewed",
      revision: "profile-rev-reviewed",
      builtin: true,
      description: "Edits the workspace; asks before shell and web.",
      rules: {
        ...baseRules,
        write_inside: "allow",
        read_outside: "ask",
        shell: "ask",
        web: "ask",
      },
      restricts: ["read_outside", "shell", "web"],
      routes: [],
    },
  ];
  const calls: Array<{
    path: string;
    method: string;
    body?: Record<string, unknown>;
    query?: Record<string, string>;
  }> = [];
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
    calls.push({
      path,
      method,
      body,
      ...(path.startsWith("/api/adapters/") && path.endsWith("/models")
        ? { query: Object.fromEntries(url.searchParams.entries()) }
        : {}),
    });

    if (path === "/api/account") {
      await respond(route, { username: "admin", must_change_password: false });
      return;
    }
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
      await respond(route, { workspaces: workspacePayloads });
      return;
    }
    if (path === "/api/events") {
      await respond(route, { events: [] });
      return;
    }
    if (path === "/api/approvals") {
      const decided = url.searchParams.get("decided_by");
      const all = [
        {
          interaction_id: "int_1",
          run_id: "run_1",
          kind: "command",
          title: "Run npm test",
          choice_label: "Allow once",
          choice_id: "allow",
          decided_by: "chatgpt",
          reason: "Matches the handoff",
          decided: "2026-05-01T10:00:00Z",
        },
        {
          interaction_id: "int_2",
          run_id: "run_1",
          kind: "command",
          title: "Delete build folder",
          choice_label: "Deny",
          choice_id: "deny",
          decided_by: "owner",
          reason: "Not wanted",
          decided: "2026-05-01T10:05:00Z",
        },
      ];
      await respond(route, {
        decisions: decided
          ? all.filter((item) => item.decided_by === decided)
          : all,
        next_offset: null,
      });
      return;
    }
    if (path === `/api/workspaces/${WS_ID}/runs` && method === "POST") {
      await respond(route, { ...snapshotRun, request_id: "manager-x" }, 201);
      return;
    }
    if (
      (path === `/api/runs/${snapshotRun.run_id}/answer` ||
        path === `/api/runs/${snapshotRun.run_id}/input`) &&
      method === "POST"
    ) {
      await respond(route, snapshotRun);
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
    if (path === "/api/nodes/pairings" && method === "POST") {
      pairings.push(body || {});
      await respond(
        route,
        {
          pairing: "wbpair1:wss://bridge.tailnet:8767#cafe.0123456789abcdef",
          name: String(body?.name),
          expires: "2026-09-25T09:00:00Z",
          note: "Single use, valid for 10 minutes.",
        },
        201,
      );
      return;
    }
    const nodeTestMatch = path.match(
      /^\/api\/nodes\/(node_[0-9a-f]{24})\/test$/,
    );
    if (nodeTestMatch && method === "POST") {
      await respond(route, { success: true, protocol: 2 });
      return;
    }
    const nodePluginsMatch = path.match(
      /^\/api\/nodes\/(node_[0-9a-f]{24})\/plugins$/,
    );
    if (nodePluginsMatch && method === "GET") {
      await respond(route, { plugins });
      return;
    }
    const nodeAdaptersMatch = path.match(
      /^\/api\/nodes\/(node_[0-9a-f]{24})\/adapters$/,
    );
    if (nodeAdaptersMatch && method === "GET") {
      await respond(route, {
        node_id: nodeAdaptersMatch[1],
        adapters: adapters
          .filter((item) => item.node_id === nodeAdaptersMatch[1])
          .map((item) => ({
            id: item.id,
            plugin: item.runtime_type,
            projects_root: item.projects_root,
            settings: item.settings,
          })),
      });
      return;
    }
    if (path === "/api/profiles" && method === "GET") {
      await respond(route, { profiles: bridgeProfiles });
      return;
    }
    const profileMatch = path.match(/^\/api\/profiles\/([a-z][a-z0-9_-]*)$/);
    if (profileMatch && method === "PUT") {
      const id = profileMatch[1];
      const existing = bridgeProfiles.find((item) => item.id === id);
      const rules = body?.rules as typeof baseRules;
      if (existing && body?.expected_revision !== existing.revision) {
        await respond(
          route,
          { error: "Profile changed since it was read" },
          409,
        );
        return;
      }
      const loosened =
        existing &&
        !body?.confirm &&
        (existing.rules as typeof baseRules).shell === "ask" &&
        rules.shell === "allow"
          ? ["shell"]
          : [];
      if (loosened.length) {
        await respond(route, {
          saved: false,
          needs_confirmation: true,
          id,
          loosened,
          routes: existing.routes,
          unenforceable_routes: [],
        });
        return;
      }
      const saved = {
        id,
        revision: `profile-rev-${id}-${bridgeProfiles.length}`,
        builtin: false,
        rules,
        restricts: [],
        routes: existing?.routes || [],
      };
      if (existing) Object.assign(existing, saved);
      else bridgeProfiles.push(saved);
      await respond(route, { saved: true, ...saved });
      return;
    }
    if (profileMatch && method === "DELETE") {
      const at = bridgeProfiles.findIndex(
        (item) => item.id === profileMatch[1],
      );
      if (at >= 0) bridgeProfiles.splice(at, 1);
      await respond(route, { deleted: true, id: profileMatch[1] });
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
      const plugin = plugins.find((item) => item.id === body?.plugin);
      const created = adapter(
        "adapter_444444444444444444444444",
        String(body?.name),
        plugin?.runtime_type || "pi",
        "new-default",
        String(body?.node_id || LOCAL_NODE),
      );
      created.projects_root = String(body?.projects_root || "/Projects");
      created.settings = (body?.settings as Record<string, string>) || {};
      adapters.push(created);
      await respond(route, publicAdapter(created), 201);
      return;
    }
    if (path === "/api/adapters/test" && method === "POST") {
      tests.push(body || {});
      const row = adapters.find((item) => item.id === body?.adapter_id);
      await respond(route, {
        success: true,
        adapter_id: body?.adapter_id,
        runtime_type: row?.runtime_type,
        native_runtime: row?.runtime_type,
        native_version: "2026.09",
        adapter_version: "1.0.0",
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
        await respond(route, publicAdapter(row));
        return;
      }
      if (!leaf && method === "GET") {
        await respond(route, { ...publicAdapter(row), security: row.security });
        return;
      }
      if (leaf === "models" && method === "GET") {
        const discoveryWorkspace = workspacePayloads.find(
          (item) => item.id === url.searchParams.get("workspace_id"),
        );
        if (
          !discoveryWorkspace?.enabled ||
          discoveryWorkspace.node_id !== row.node_id
        ) {
          await respond(
            route,
            {
              error: "Model discovery requires an enabled same-Node workspace",
            },
            400,
          );
          return;
        }
        const model =
          id === LOCAL_PI
            ? {
                selector: "pi-local-fast",
                displayName: "Local Pi model",
                reasoningOptions: ["low", "high"],
              }
            : id === GPU_PI || id === REMOTE_PI
              ? {
                  selector: row.model_policy.default,
                  displayName: `${row.name} model`,
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
      if (leaf === "usage-limits" && method === "GET") {
        if (id !== LOCAL_CODEX) {
          await respond(
            route,
            {
              error: "Adapter does not report usage limits",
              code: "runtime_unsupported",
            },
            501,
          );
          return;
        }
        if (options.codexQuota === "error") {
          await respond(
            route,
            {
              error: "Runtime adapter is unavailable",
              code: "runtime_unavailable",
            },
            400,
          );
          return;
        }
        if (options.codexQuotaDelayMs) {
          await page.waitForTimeout(options.codexQuotaDelayMs);
        }
        if (options.codexQuota === "empty") {
          await respond(route, {
            available: false,
            ordinaryUsageAllowed: null,
            buckets: [],
          });
          return;
        }
        if (options.codexQuota === "ordinary-unavailable") {
          await respond(route, {
            available: true,
            ordinaryUsageAllowed: false,
            buckets: [
              {
                limitId: "codex",
                limitName: "Codex",
                planType: "pro",
                windows: [],
              },
            ],
          });
          return;
        }
        if (options.codexQuota === "same-duration") {
          await respond(route, {
            available: true,
            ordinaryUsageAllowed: true,
            buckets: [
              {
                limitId: "base_model_inference",
                limitName: "gpt-reserve",
                planType: "prolite",
                windows: [
                  {
                    usedPercent: 70,
                    remainingPercent: 30,
                    windowDurationMins: 10080,
                    resetsAt: 1790909226,
                  },
                ],
              },
              {
                limitId: "codex",
                limitName: null,
                planType: "prolite",
                windows: [
                  {
                    usedPercent: 62,
                    remainingPercent: 38,
                    windowDurationMins: 10080,
                    resetsAt: 1791073029,
                  },
                ],
              },
            ],
          });
          return;
        }
        await respond(route, {
          available: true,
          ordinaryUsageAllowed: true,
          buckets: [
            {
              limitId: "codex",
              limitName: "Codex",
              planType: "pro",
              windows: [
                {
                  usedPercent: 31,
                  remainingPercent: 69,
                  windowDurationMins: 300,
                  resetsAt: 1800000000,
                },
                {
                  usedPercent: 39,
                  remainingPercent: 61,
                  windowDurationMins: 10080,
                },
              ],
            },
          ],
        });
        return;
      }
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
  return { calls, tests, updates, adapters, pairings };
}

async function navigate(page: Page, title: string) {
  await page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("button", { name: title, exact: true })
    .click();
}

test("same-runtime adapter instances stay distinct in workspaces and handoff targets", async ({
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
  await target.selectOption(GPU_PI);
  await expect(page.getByRole("button", { name: "Start run" })).toHaveCount(0);
  expect(
    api.calls.some(
      (call) => call.method === "POST" && call.path.endsWith("/runs"),
    ),
  ).toBe(false);
  expect(errors).toEqual([]);
});

test("adapter forms pick a Node plugin and keep no URL or token", async ({
  page,
}) => {
  const api = await mockApi(page);
  await page.goto("./");
  await navigate(page, "Adapters");

  await page.getByRole("button", { name: "Add adapter" }).click();
  await page.getByLabel("Name", { exact: true }).fill("Backup Pi");
  await expect(page.getByLabel("Base URL")).toHaveCount(0);
  await expect(page.getByLabel("Adapter token")).toHaveCount(0);
  await page.getByLabel("Agent plugin", { exact: true }).selectOption("pi");
  await page.getByLabel("pi_binary", { exact: true }).fill("/opt/pi/bin/pi");
  await page
    .getByRole("button", { name: "Add adapter", exact: true })
    .last()
    .click();
  await expect(
    page.getByRole("heading", { name: "Backup Pi · Pi" }),
  ).toBeVisible();
  expect(
    api.calls.find((c) => c.path === "/api/adapters" && c.method === "POST")
      ?.body,
  ).toEqual({
    name: "Backup Pi",
    node_id: LOCAL_NODE,
    plugin: "pi",
    settings: { pi_binary: "/opt/pi/bin/pi" },
    enabled: true,
  });

  const gpuRow = page
    .locator(".adapter-card")
    .filter({ hasText: "GPU Pi · Pi" });
  await gpuRow.getByRole("button", { name: "Edit" }).click();
  await expect(page.getByLabel("Projects root")).toHaveValue("/Projects");
  await page.getByRole("button", { name: "Test connection" }).click();
  await expect(
    page.getByRole("status").filter({ hasText: "Connected: pi" }),
  ).toBeVisible();
  expect(api.tests.at(-1)).toEqual({ adapter_id: GPU_PI });
  await page.getByLabel("Name", { exact: true }).fill("GPU Pi renamed");
  await page.getByRole("button", { name: "Save adapter" }).click();
  await expect(
    page.getByRole("heading", { name: "GPU Pi renamed · Pi" }),
  ).toBeVisible();
  expect(api.updates.at(-1)).toEqual({
    id: GPU_PI,
    body: {
      name: "GPU Pi renamed",
      projects_root: "/Projects",
      settings: {},
      enabled: true,
    },
  });
});

test("model discovery uses each adapter ID", async ({ page }) => {
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
});

test("model discovery and policy save use only enabled workspaces on the adapter Node", async ({
  page,
}) => {
  const api = await mockApi(page, {
    remoteAdapter: true,
    remoteWorkspace: true,
  });
  await page.goto("./");
  await navigate(page, "Adapters");

  for (const target of [
    {
      id: REMOTE_PI,
      name: "GPU-only Pi",
      workspaceId: "ws_cccccccccccccccccccccccc",
      workspaceName: "GPU archive",
    },
    {
      id: LOCAL_PI,
      name: "Local Pi",
      workspaceId: WS_ID,
      workspaceName: "Alpine archive",
    },
  ]) {
    await page
      .locator(".adapter-card")
      .filter({ hasText: `${target.name} · Pi` })
      .getByRole("button", { name: "Models" })
      .click();
    const discovery = page.getByLabel("Discovery workspace");
    await expect(discovery.locator("option")).toHaveCount(1);
    await expect(discovery.locator("option")).toHaveText(target.workspaceName);
    await expect(discovery).toHaveValue(target.workspaceId);
    await expect(
      page.getByRole("checkbox", { name: new RegExp(`${target.name} model`) }),
    ).toBeVisible();
    const modelCalls = api.calls.filter(
      (call) => call.path === `/api/adapters/${target.id}/models`,
    );
    expect(modelCalls.length).toBeGreaterThan(0);
    expect(
      modelCalls.every(
        (call) => call.query?.workspace_id === target.workspaceId,
      ),
    ).toBe(true);

    await page.getByRole("button", { name: "Save model policy" }).click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    expect(
      api.calls.find(
        (call) =>
          call.path === `/api/adapters/${target.id}/model-policy` &&
          call.method === "POST",
      )?.body,
    ).toMatchObject({ workspace_id: target.workspaceId });
  }
});

for (const disabledWorkspace of [false, true]) {
  test(`model discovery stays empty when the adapter Node has ${disabledWorkspace ? "only a disabled workspace" : "no workspace"}`, async ({
    page,
  }) => {
    const api = await mockApi(page, {
      remoteAdapter: true,
      remoteWorkspace: disabledWorkspace,
      remoteWorkspaceEnabled: false,
    });
    await page.goto("./");
    await navigate(page, "Adapters");
    await page
      .locator(".adapter-card")
      .filter({ hasText: "GPU-only Pi · Pi" })
      .getByRole("button", { name: "Models" })
      .click();

    await expect(
      page.getByRole("heading", { name: "Enable a workspace on this Node" }),
    ).toBeVisible();
    await expect(page.getByLabel("Discovery workspace")).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: "Save model policy" }),
    ).toBeDisabled();
    await page.getByRole("button", { name: "Cancel" }).click();
    expect(
      api.calls.filter(
        (call) =>
          call.path === `/api/adapters/${REMOTE_PI}/models` ||
          (call.path === `/api/adapters/${REMOTE_PI}/model-policy` &&
            call.method === "POST"),
      ),
    ).toEqual([]);
  });
}

test("Codex security dialog says which profile keys the agent cannot enforce", async ({
  page,
}) => {
  await mockApi(page);
  await page.goto("./");
  await navigate(page, "Workspaces");
  const codexTarget = page.locator(".target-row").filter({
    hasText: "Local Codex · Codex",
  });
  await codexTarget.getByRole("button", { name: "Change security" }).click();
  await page.getByText("Use a Workspace Bridge profile").click();
  const profiles = page.getByRole("group", {
    name: "Workspace Bridge profiles",
  });
  await expect(
    profiles.getByRole("button", { name: /reviewed/ }),
  ).toBeDisabled();
  await expect(
    profiles.getByText(/This agent cannot enforce read_outside, shell, web/),
  ).toBeVisible();
  await expect(
    profiles.getByRole("button", { name: /read-only/ }),
  ).toBeEnabled();
});

test("Codex security dialog lets a route follow the agent's own security", async ({
  page,
}) => {
  const api = await mockApi(page);
  await page.goto("./");
  await navigate(page, "Workspaces");
  const codexTarget = page.locator(".target-row").filter({
    hasText: "Local Codex · Codex",
  });
  await codexTarget.getByRole("button", { name: "Change security" }).click();
  await expect(
    page.getByRole("radio", { name: /Use the agent's own security/ }),
  ).toBeChecked();
  await page.getByText("Use a Workspace Bridge profile").click();
  await page.getByRole("button", { name: /read-only/ }).click();
  await page.getByRole("button", { name: "Save security source" }).click();
  expect(
    api.calls.find(
      (call) =>
        call.path === `/api/workspaces/${WS_ID}/routes/${LOCAL_CODEX}` &&
        call.method === "POST",
    )?.body,
  ).toMatchObject({ security_source: "profile", profile_id: "read-only" });
});

test("Codex adapter card shows current account quota on the Adapters page", async ({
  page,
}) => {
  const api = await mockApi(page);
  await page.goto("./");
  await navigate(page, "Adapters");
  const codexCard = page
    .locator(".adapter-card")
    .filter({ hasText: "Local Codex · Codex" });
  await expect(codexCard.getByText("Codex quota")).toBeVisible();
  await expect(codexCard.getByText("69% left")).toBeVisible();
  await expect(codexCard.getByText("61% left")).toBeVisible();
  // Window labels carry their bucket identity as well as their duration.
  await expect(codexCard.getByText("Codex · 5h")).toBeVisible();
  await expect(codexCard.getByText("Codex · 7d")).toBeVisible();
  // Both windows come from one bucket; the meter names carry the bucket
  // label, never a mislabeled primary/secondary meaning.
  const meters = codexCard.getByRole("meter");
  await expect(meters).toHaveCount(2);
  await expect(meters.nth(0)).toHaveAccessibleName(
    "Codex · 5h remaining percent",
  );
  await expect(meters.nth(1)).toHaveAccessibleName(
    "Codex · 7d remaining percent",
  );
  // Quota is fetched per adapter, only for adapters advertising usageLimits.
  const quotaCalls = api.calls.filter((call) =>
    call.path.endsWith("/usage-limits"),
  );
  expect(quotaCalls).toHaveLength(1);
  expect(quotaCalls[0]?.path).toBe(`/api/adapters/${LOCAL_CODEX}/usage-limits`);
  expect(quotaCalls[0]?.method).toBe("GET");
  // Pi adapter cards gain no quota block.
  const piCard = page
    .locator(".adapter-card")
    .filter({ hasText: "Local Pi · Pi" });
  await expect(piCard.getByText("Codex quota")).toHaveCount(0);
});

test("delayed quota response survives an unrelated global refresh", async ({
  page,
}) => {
  const api = await mockApi(page, { codexQuotaDelayMs: 1500 });
  await page.goto("./");
  await navigate(page, "Adapters");
  const codexCard = page
    .locator(".adapter-card")
    .filter({ hasText: "Local Codex · Codex" });
  await expect
    .poll(
      () =>
        api.calls.filter((call) => call.path.endsWith("/usage-limits")).length,
    )
    .toBe(1);
  await expect(codexCard.getByText("Loading quota…")).toBeVisible();
  const statusCallsBefore = api.calls.filter(
    (call) => call.path === "/api/status",
  ).length;
  // Trigger an unrelated global refresh while the quota read is in flight.
  await page.getByRole("button", { name: "Refresh" }).click();
  // The eventual quota response must still render; the UI cannot remain in
  // its loading state or require a manual request.
  await expect(codexCard.getByText("69% left")).toBeVisible();
  await expect(codexCard.getByText("Loading quota…")).toHaveCount(0);
  const statusCallsAfter = api.calls.filter(
    (call) => call.path === "/api/status",
  ).length;
  expect(statusCallsAfter).toBeGreaterThan(statusCallsBefore);
  expect(
    api.calls.filter((call) => call.path.endsWith("/usage-limits")).length,
  ).toBe(1);
});

test("equal-duration quota windows keep bucket identities visible", async ({
  page,
}) => {
  const api = await mockApi(page, { codexQuota: "same-duration" });
  await page.goto("./");
  await navigate(page, "Adapters");
  const codexCard = page
    .locator(".adapter-card")
    .filter({ hasText: "Local Codex · Codex" });
  await expect(codexCard.getByText("gpt-reserve · 7d")).toBeVisible();
  await expect(codexCard.getByText("codex · 7d")).toBeVisible();
  await expect(codexCard.getByText("30% left")).toBeVisible();
  await expect(codexCard.getByText("38% left")).toBeVisible();
  await expect(
    await codexCard.locator(".quota-label").allTextContents(),
  ).toEqual(["gpt-reserve · 7d", "codex · 7d"]);
  const meters = codexCard.getByRole("meter");
  await expect(meters).toHaveCount(2);
  await expect(meters.nth(0)).toHaveAccessibleName(
    "gpt-reserve · 7d remaining percent",
  );
  await expect(meters.nth(1)).toHaveAccessibleName(
    "codex · 7d remaining percent",
  );
  expect(
    api.calls.filter((call) => call.path.endsWith("/usage-limits")).length,
  ).toBe(1);
});

test("quota stays unfetched outside the Adapters section and reports unavailable cleanly", async ({
  page,
}) => {
  const api = await mockApi(page, { codexQuota: "error" });
  await page.goto("./");
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
  await expect(
    api.calls.filter((call) => call.path.endsWith("/usage-limits")),
  ).toHaveLength(0);
  await navigate(page, "Adapters");
  const codexCard = page
    .locator(".adapter-card")
    .filter({ hasText: "Local Codex · Codex" });
  await expect(codexCard.getByText(/Quota unavailable/)).toBeVisible();
  await expect(codexCard.getByText("69% left")).toHaveCount(0);
  // The rest of the Adapters page still works despite the quota failure.
  await expect(codexCard.getByText("Model policy")).toBeVisible();
  const quotaCalls = api.calls.filter((call) =>
    call.path.endsWith("/usage-limits"),
  );
  expect(quotaCalls).toHaveLength(1);
});

test("empty quota data reads as unavailable, not as zero remaining", async ({
  page,
}) => {
  const api = await mockApi(page, { codexQuota: "empty" });
  await page.goto("./");
  await navigate(page, "Adapters");
  const codexCard = page
    .locator(".adapter-card")
    .filter({ hasText: "Local Codex · Codex" });
  await expect(codexCard.getByText(/Quota unavailable/)).toBeVisible();
  await expect(codexCard.getByText(/0% left/)).toHaveCount(0);
  expect(
    api.calls.some(
      (call) =>
        call.path === `/api/adapters/${LOCAL_CODEX}/usage-limits` &&
        call.method === "GET",
    ),
  ).toBe(true);
});

test("ordinary-usage-unavailable state survives absent windows", async ({
  page,
}) => {
  const api = await mockApi(page, { codexQuota: "ordinary-unavailable" });
  await page.goto("./");
  await navigate(page, "Adapters");
  const codexCard = page
    .locator(".adapter-card")
    .filter({ hasText: "Local Codex · Codex" });
  // The explicit backend state is surfaced even with zero window rows.
  await expect(
    codexCard.getByText("Ordinary usage unavailable."),
  ).toBeVisible();
  // No percentage rows are invented for absent windows.
  await expect(codexCard.getByRole("meter")).toHaveCount(0);
  await expect(codexCard.getByText(/% left/)).toHaveCount(0);
  await expect(codexCard.getByText(/Quota unavailable/)).toHaveCount(0);
  expect(
    api.calls.some(
      (call) =>
        call.path === `/api/adapters/${LOCAL_CODEX}/usage-limits` &&
        call.method === "GET",
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

test("Nodes are paired with a one-time string and hold no URL or token", async ({
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
  await expect(page.getByText("Embedded in the Bridge")).toBeVisible();

  await page.getByRole("button", { name: "Pair a Node" }).first().click();
  await expect(page.getByLabel("Node URL")).toHaveCount(0);
  await expect(page.getByLabel("Node token")).toHaveCount(0);
  await page.getByLabel("Name", { exact: true }).fill("Laptop");
  await page.getByRole("button", { name: "Create pairing string" }).click();
  await expect(page.getByLabel("Pairing string")).toHaveValue(
    "wbpair1:wss://bridge.tailnet:8767#cafe.0123456789abcdef",
  );
  expect(api.pairings).toEqual([{ name: "Laptop", replace: false }]);
  await page.getByRole("button", { name: "Done" }).click();

  const gpuNode = page.locator(".node-card").filter({ hasText: "GPU Server" });
  await expect(gpuNode.getByText(/Last connected/)).toBeVisible();
  await gpuNode.getByRole("button", { name: "Edit" }).first().click();
  await page.getByRole("button", { name: "Test connection" }).click();
  await expect(page.getByRole("status")).toHaveText(
    "Connected: Node Protocol is ready.",
  );
  await page.getByRole("button", { name: "Cancel" }).click();
  await gpuNode.getByRole("button", { name: "Add adapter" }).click();
  await expect(page.getByLabel("Adapter token")).toHaveCount(0);
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
  await expect(page.getByRole("button", { name: "Start run" })).toHaveCount(0);
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

test("run messages show receipts and Manual control steers by hand", async ({
  page,
}) => {
  const mock = await mockApi(page, { runSnapshot: true, activeRun: true });
  await page.goto("./");
  await navigate(page, "Runs");
  await page.getByRole("button", { name: "Open run" }).click();
  const receipts = page.locator(".run-input-receipts");
  await expect(receipts).toContainText("queued");
  await expect(receipts).toContainText("delivered");
  await expect(
    page.getByText("ChatGPT has not called the Bridge since it started."),
  ).toBeHidden();
  await page.getByText("Manual control").click();
  await expect(
    page.getByText("ChatGPT has not called the Bridge since it started."),
  ).toBeVisible();
  await page.getByLabel("Instruction").fill("Skip the slow tests.");
  await page.getByRole("button", { name: "Send instruction" }).click();
  await expect
    .poll(
      () =>
        mock.calls.find((call) => call.path === "/api/runs/run-snapshot/input")
          ?.body,
    )
    .toMatchObject({ mode: "steer", instruction: "Skip the slow tests." });
  const sent = mock.calls.find(
    (call) => call.path === "/api/runs/run-snapshot/input",
  )?.body;
  expect(String(sent?.request_id)).toMatch(/^[a-z0-9]{24}$/);
});

test("a waiting request can be answered by hand after a confirmation", async ({
  page,
}) => {
  const mock = await mockApi(page, {
    runSnapshot: true,
    activeRun: true,
    pendingRequest: true,
  });
  await page.goto("./");
  await navigate(page, "Runs");
  await page.getByRole("button", { name: "Open run" }).click();
  await page.getByText("Answer by hand").click();
  await page.getByLabel("Reason (optional)").fill("Safe test run");
  await page.getByRole("button", { name: "Allow once" }).click();
  await expect(
    page.getByRole("alertdialog").getByText(/recorded as decided/),
  ).toBeVisible();
  expect(
    mock.calls.some((call) => call.path === "/api/runs/run-snapshot/answer"),
  ).toBe(false);
  await page.getByRole("button", { name: "Continue" }).click();
  await expect
    .poll(
      () =>
        mock.calls.find((call) => call.path === "/api/runs/run-snapshot/answer")
          ?.body,
    )
    .toEqual({
      interaction_id: "int_pending",
      reason: "Safe test run",
      choice_id: "allow",
    });
});

test("Start run by hand posts the chosen workspace, agent and handoff", async ({
  page,
}) => {
  const mock = await mockApi(page);
  await page.goto("./");
  await navigate(page, "Runs");
  await page.getByRole("button", { name: "Start run by hand" }).click();
  await page.getByLabel("Agent", { exact: true }).selectOption(LOCAL_PI);
  await expect(page.getByLabel("Handoff", { exact: true })).toHaveValue(JOB_ID);
  await page.getByRole("button", { name: "Start run", exact: true }).click();
  await expect
    .poll(
      () =>
        mock.calls.find(
          (call) =>
            call.path === `/api/workspaces/${WS_ID}/runs` &&
            call.method === "POST",
        )?.body,
    )
    .toMatchObject({
      adapter_id: LOCAL_PI,
      job_id: JOB_ID,
      isolation: "none",
    });
});

test("admin login requires a password change and supports later changes", async ({
  page,
}) => {
  let signedIn = false;
  let mustChange = true;
  let password = "admin";
  await page.route("**/api/account", async (route) => {
    await route.fulfill({
      status: signedIn ? 200 : 401,
      contentType: "application/json",
      body: JSON.stringify(
        signedIn
          ? { username: "admin", must_change_password: mustChange }
          : { error: "Admin sign-in required" },
      ),
    });
  });
  await page.route("**/api/login", async (route) => {
    const body = route.request().postDataJSON();
    signedIn = body.username === "admin" && body.password === password;
    await route.fulfill({
      status: signedIn ? 200 : 401,
      contentType: "application/json",
      body: JSON.stringify(
        signedIn
          ? { must_change_password: mustChange }
          : { error: "Incorrect username or password" },
      ),
    });
  });
  await page.route("**/api/account/password", async (route) => {
    const body = route.request().postDataJSON();
    expect(body.current_password).toBe(password);
    password = body.new_password;
    mustChange = false;
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ must_change_password: false }),
    });
  });
  await page.route("**/api/status", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        version: "0.1.1",
        bridge: { configured: false, enabled: false },
        nodes: [],
        adapters: { adapters: [] },
      }),
    }),
  );
  await page.route("**/api/diagnostics*", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ checks: [], runnable_routes: [] }),
    }),
  );
  await page.route("**/api/workspaces", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ workspaces: [] }),
    }),
  );
  await page.route("**/api/events", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ events: [] }),
    }),
  );
  await page.route("**/api/runs", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ runs: [] }),
    }),
  );
  await page.goto("/static/dist/");
  await expect(page.getByLabel("Username")).toHaveValue("");
  await page.getByLabel("Username").fill("admin");
  await page.getByLabel("Password", { exact: true }).fill("wrong");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("alert")).toHaveText(
    "Incorrect username or password",
  );
  await page.getByLabel("Password", { exact: true }).fill("admin");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Replace the temporary password" }),
  ).toBeVisible();
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "Replace the temporary password" }),
  ).toBeVisible();
  await page.getByLabel("Current password", { exact: true }).fill("admin");
  await page
    .getByLabel("New password", { exact: true })
    .fill("browser-password");
  await page
    .getByLabel("Confirm new password", { exact: true })
    .fill("different-password");
  await page.getByRole("button", { name: "Save password" }).click();
  await expect(page.getByRole("alert")).toHaveText(
    "New passwords do not match.",
  );
  await page
    .getByLabel("Confirm new password", { exact: true })
    .fill("browser-password");
  await page.getByRole("button", { name: "Save password" }).click();
  await page
    .getByRole("button", { name: "Change password", exact: true })
    .click();
  await page
    .getByLabel("Current password", { exact: true })
    .fill("browser-password");
  await page
    .getByLabel("New password", { exact: true })
    .fill("changed-again-password");
  await page
    .getByLabel("Confirm new password", { exact: true })
    .fill("changed-again-password");
  await page.getByRole("button", { name: "Save password" }).click();
  await expect(
    page.getByRole("button", { name: "Change password", exact: true }),
  ).toBeVisible();
});

test("OpenCode can be registered from its Node plugin", async ({ page }) => {
  const api = await mockApi(page);
  await page.goto("./");
  await navigate(page, "Adapters");
  await page.getByRole("button", { name: "Add adapter" }).click();
  await page.getByLabel("Name", { exact: true }).fill("Local OpenCode");
  await page
    .getByLabel("Agent plugin", { exact: true })
    .selectOption("opencode");
  await page
    .getByLabel("binary", { exact: true })
    .fill("/usr/local/bin/opencode");
  await page
    .getByRole("button", { name: "Add adapter", exact: true })
    .last()
    .click();
  await expect(
    page.getByRole("heading", { name: "Local OpenCode · OpenCode" }),
  ).toBeVisible();
  expect(
    api.calls.find((c) => c.path === "/api/adapters" && c.method === "POST")
      ?.body,
  ).toMatchObject({
    plugin: "opencode",
    settings: { binary: "/usr/local/bin/opencode" },
  });
});

test("Audit lists the approval log and filters it by who decided", async ({
  page,
}) => {
  await mockApi(page);
  await page.goto("./");
  await navigate(page, "Audit");
  await expect(page.getByText("Run npm test")).toBeVisible();
  await expect(page.getByText("Delete build folder")).toBeVisible();
  await page.getByLabel("Decided by").selectOption("chatgpt");
  await expect(page.getByText("Delete build folder")).toHaveCount(0);
  await expect(page.getByText(/decided by ChatGPT: Matches/)).toBeVisible();
});

test("Profiles lists the shared profiles and shows a profile's rules", async ({
  page,
}) => {
  const api = await mockApi(page);
  await page.goto("./");
  await navigate(page, "Adapters");
  const list = page.getByRole("complementary", { name: "Profiles" });
  await expect(list.getByRole("button", { name: /read-only/ })).toBeVisible();
  await expect(list.getByRole("button", { name: /reviewed/ })).toBeVisible();
  const rules = page.getByRole("table", { name: "Rules" });
  await expect(
    rules.getByRole("row", { name: /Run shell commands/ }),
  ).toContainText("deny");
  await expect(
    page.getByText("Used by 1 route: Alpha · Local Codex"),
  ).toBeVisible();
  expect(
    api.calls.some(
      (call) =>
        call.path.startsWith("/api/adapters/") &&
        call.path.endsWith("/profiles"),
    ),
  ).toBe(false);
  await expect(page.getByText(/HTTP 404/)).toHaveCount(0);
});

test("a custom profile is created from a built-in one, edited with confirmation, and deleted", async ({
  page,
}) => {
  const api = await mockApi(page);
  await page.goto("./");
  await navigate(page, "Adapters");
  await page.getByRole("button", { name: /^reviewed/ }).click();
  await page.getByRole("button", { name: "Create from selected" }).click();
  await page.getByLabel("Profile ID").fill("careful");
  await page
    .getByLabel("Workspace-relative globs, one per line")
    .fill("secrets/**");
  await page.getByRole("button", { name: "Add command rule" }).click();
  await page.getByLabel("Command 1 prefix").fill("git push");
  await page.locator("#command-mode-0").selectOption("deny");
  await page.getByRole("button", { name: "Create profile" }).click();
  await expect(page.getByText("Profile careful created.")).toBeVisible();
  const created = api.calls.find(
    (call) => call.path === "/api/profiles/careful" && call.method === "PUT",
  );
  expect(created?.body).toMatchObject({
    expected_revision: null,
    confirm: false,
    rules: {
      shell: "ask",
      protected: ["secrets/**"],
      commands: [{ prefix: "git push", mode: "deny" }],
      external_roots: [],
    },
  });

  await page.getByRole("button", { name: "Edit rules" }).click();
  await page.locator("#rule-shell").selectOption("allow");
  await page.getByRole("button", { name: "Save profile" }).click();
  await expect(
    page.getByRole("alertdialog").getByText(/This loosens Run shell commands/),
  ).toBeVisible();
  await page.getByRole("button", { name: "Save anyway" }).click();
  await expect(page.getByText("Profile careful updated.")).toBeVisible();
  const puts = api.calls.filter(
    (call) => call.path === "/api/profiles/careful" && call.method === "PUT",
  );
  expect(puts.map((call) => call.body?.confirm)).toEqual([false, false, true]);

  await page
    .getByRole("region", { name: "Profile controls" })
    .getByRole("button", { name: "Delete", exact: true })
    .click();
  await page.getByRole("button", { name: "Delete profile" }).click();
  await expect(page.getByText("Profile careful deleted.")).toBeVisible();
  await expect(page.getByRole("button", { name: /^careful/ })).toHaveCount(0);
});

test("built-in profiles cannot be edited or deleted", async ({ page }) => {
  await mockApi(page);
  await page.goto("./");
  await navigate(page, "Adapters");
  await page.getByRole("button", { name: /^reviewed/ }).click();
  await expect(page.getByRole("button", { name: "Edit rules" })).toHaveCount(0);
  await expect(
    page
      .getByRole("region", { name: "Profile controls" })
      .getByRole("button", { name: "Delete", exact: true }),
  ).toHaveCount(0);
});
