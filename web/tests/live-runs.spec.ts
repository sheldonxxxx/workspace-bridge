import { expect, test, type Page, type Route } from "@playwright/test";

const ALPHA = "run_aaaaaaaaaaaaaaaaaaaaaaaa";
const BETA = "run_bbbbbbbbbbbbbbbbbbbbbbbb";
const WORKSPACE = "ws_aaaaaaaaaaaaaaaaaaaaaaaa";
const created = "2026-10-11T08:00:00Z";

type TestRun = ReturnType<typeof run>;
type FeedItem = {
  id: string;
  kind: string;
  status: string;
  created: string;
  details: { title: string; result: { output_preview: string } };
};
type Execution = {
  execution_id: string;
  tool: string;
  state: string;
  output_preview: string;
};
type Cursor = { created: string; id: string };
type FeedPage = {
  activities?: FeedItem[];
  executions?: Execution[];
  next_cursor: Cursor | null;
};
type FakeSource = EventTarget & {
  url: string;
  readyState: number;
  closed: boolean;
  listenerCount: number;
};
type StreamingWindow = Window & { __runSources: FakeSource[] };

function run(id: string, title: string) {
  return {
    run_id: id,
    handoff_title: title,
    workspace_id: WORKSPACE,
    workspace_name: "Alpine archive",
    adapter_name: "Local Pi",
    runtime_type: "pi",
    phase: "active",
    active_state: "running",
    outcome: "",
    created,
    model: `${title} model`,
  };
}
function activity(id: string, title: string): FeedItem {
  return {
    id,
    kind: "tool",
    status: "completed",
    created,
    details: { title, result: { output_preview: `${title} output` } },
  };
}
function execution(id: string, title: string): Execution {
  return {
    execution_id: id,
    tool: title,
    state: "completed",
    output_preview: `${title} output`,
  };
}

async function mockApi(page: Page) {
  await page.addInitScript(() => {
    class FakeEventSource extends EventTarget {
      static CONNECTING = 0;
      static OPEN = 1;
      static CLOSED = 2;
      readyState = 1;
      closed = false;
      listenerCount = 0;
      constructor(public url: string) {
        super();
        (
          window as unknown as { __runSources: FakeEventSource[] }
        ).__runSources.push(this);
        queueMicrotask(() => this.dispatchEvent(new Event("open")));
      }
      addEventListener(...args: Parameters<EventTarget["addEventListener"]>) {
        this.listenerCount++;
        super.addEventListener(...args);
      }
      removeEventListener(
        ...args: Parameters<EventTarget["removeEventListener"]>
      ) {
        this.listenerCount--;
        super.removeEventListener(...args);
      }
      close() {
        this.closed = true;
        this.readyState = 2;
      }
    }
    (window as unknown as { __runSources: FakeEventSource[] }).__runSources =
      [];
    window.EventSource = FakeEventSource as unknown as typeof EventSource;
    // Feed pagination is driven explicitly in these regression tests.
    window.IntersectionObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof IntersectionObserver;
  });
  const snapshots = new Map<string, TestRun>([
    [ALPHA, run(ALPHA, "Alpha")],
    [BETA, run(BETA, "Beta")],
  ]);
  const activities = new Map<string, FeedPage>([
    [ALPHA, { activities: [], next_cursor: null }],
    [BETA, { activities: [], next_cursor: null }],
  ]);
  const executions = new Map<string, FeedPage>([
    [ALPHA, { executions: [], next_cursor: null }],
    [BETA, { executions: [], next_cursor: null }],
  ]);
  const olderActivities = new Map<string, FeedPage>();
  const olderExecutions = new Map<string, FeedPage>();
  const calls: URL[] = [];
  const list = {
    first: [ALPHA, BETA],
    older: [] as string[],
    next: null as number | null,
    olderNext: null as number | null,
  };
  let detailGate: ((id: string) => Promise<void>) | undefined;
  const respond = (route: Route, body: unknown, status = 200) =>
    route.fulfill({
      status,
      contentType: "application/json",
      body: JSON.stringify(body),
    });
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    calls.push(url);
    const path = url.pathname;
    if (path === "/api/account")
      return respond(route, { must_change_password: false });
    if (path === "/api/status")
      return respond(route, {
        version: "0.2.0",
        listen_mode: "loopback",
        bridge: { configured: true, enabled: true },
        nodes: [],
        adapters: { adapters: [] },
      });
    if (path === "/api/diagnostics")
      return respond(route, {
        generated_at: created,
        mode: "live",
        overall: {
          status: "pass",
          summary: "Ready",
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
      });
    if (path === "/api/system/versions")
      return respond(route, { nodes: [], adapters: [] });
    if (path === "/api/workspaces")
      return respond(route, {
        workspaces: [
          {
            id: WORKSPACE,
            name: "Alpine archive",
            root: "/Projects/alpine",
            enabled: true,
            write_scope: "handoff",
            excludes: "[]",
            node_id: "node_local",
            node_name: "Local Mac",
            routes: {},
          },
        ],
      });
    if (path === "/api/events") return respond(route, { events: [] });
    if (path === "/api/runs") {
      const older = Number(url.searchParams.get("offset")) > 0;
      return respond(route, {
        runs: (older ? list.older : list.first).map((id) => snapshots.get(id)),
        next_offset: older ? list.olderNext : list.next,
      });
    }
    if (path === `/api/workspaces/${WORKSPACE}/jobs`)
      return respond(route, { handoffs: [] });
    if (path === `/api/workspaces/${WORKSPACE}/runs`)
      return respond(route, {
        runs: list.first.map((id) => snapshots.get(id)),
      });
    const match = path.match(
      /^\/api\/runs\/([^/]+)(?:\/(activities|executions))?$/,
    );
    if (match) {
      const [, id, feed] = match;
      if (!feed) {
        const snapshot = { ...snapshots.get(id) };
        await detailGate?.(id);
        // Aborted reads may have been removed while a prior inspector closed.
        await respond(route, snapshot).catch(() => {});
        return;
      }
      const before = url.searchParams.get("before_id");
      const pageData = before
        ? (feed === "activities" ? olderActivities : olderExecutions).get(
            before,
          )
        : (feed === "activities" ? activities : executions).get(id);
      return respond(
        route,
        pageData || { activities: [], executions: [], next_cursor: null },
      );
    }
    return respond(route, { error: `Unexpected endpoint ${path}` }, 404);
  });
  return {
    snapshots,
    activities,
    executions,
    olderActivities,
    olderExecutions,
    calls,
    list,
    setDetailGate: (gate: typeof detailGate) => {
      detailGate = gate;
    },
    count: (path: string) =>
      calls.filter((url) => url.pathname === path).length,
  };
}

async function emit(
  page: Page,
  type: "run.changed" | "reset" | "error",
  runId?: string,
  repeats = 1,
) {
  await page.evaluate(
    ({ type, runId, repeats }) => {
      const sources = (window as unknown as StreamingWindow).__runSources;
      for (const source of sources) {
        const selected = new URL(source.url, location.origin).searchParams.get(
          "run_id",
        );
        if (source.closed || (selected && runId && selected !== runId))
          continue;
        for (let index = 0; index < repeats; index++)
          source.dispatchEvent(
            type === "error"
              ? new Event("error")
              : new MessageEvent(type, {
                  data: JSON.stringify(
                    type === "run.changed" ? { run_id: runId } : {},
                  ),
                }),
          );
      }
    },
    { type, runId, repeats },
  );
}
async function openAlpha(page: Page) {
  await page.goto("./#runs");
  await page
    .locator(".run-row")
    .filter({ hasText: "Alpha" })
    .getByRole("button", { name: "Open run" })
    .click();
  await expect(
    page.getByRole("dialog").getByText("Alpha model", { exact: true }),
  ).toBeVisible();
}
async function sourceCounts(page: Page) {
  return page.evaluate(() =>
    (window as unknown as StreamingWindow).__runSources.map((source) => ({
      url: source.url,
      closed: source.closed,
      listeners: source.listenerCount,
    })),
  );
}

test("stream hints reload snapshots and run lists without refreshing diagnostics", async ({
  page,
}) => {
  const mock = await mockApi(page);
  await openAlpha(page);
  const diagnostics = mock.count("/api/diagnostics");
  const details = mock.count(`/api/runs/${ALPHA}`);
  mock.snapshots.get(ALPHA)!.model = "Updated model";
  mock.snapshots.get(ALPHA)!.phase = "terminal";
  mock.snapshots.get(ALPHA)!.outcome = "completed";
  mock.activities.set(ALPHA, {
    activities: [activity("activity-live", "Live activity")],
    next_cursor: null,
  });
  mock.executions.set(ALPHA, {
    executions: [execution("execution-live", "Live execution")],
    next_cursor: null,
  });
  await emit(page, "run.changed", ALPHA, 20);
  await expect(
    page.getByRole("dialog").getByText("Updated model", { exact: true }),
  ).toBeVisible();
  await expect(
    page.locator(".run-row").filter({ hasText: "Alpha" }),
  ).toContainText("completed");
  await page.getByRole("tab", { name: "Activities", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Live activity", exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("dialog")).toContainText("Live activity output");
  await page.getByRole("tab", { name: "Executions", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Live execution", exact: true }),
  ).toBeVisible();
  expect(mock.count(`/api/runs/${ALPHA}`) - details).toBe(1);
  expect(mock.count("/api/diagnostics")).toBe(diagnostics);
});

test("reset and return to visibility reconcile durable state", async ({
  page,
}) => {
  const mock = await mockApi(page);
  await openAlpha(page);
  mock.snapshots.get(ALPHA)!.model = "Reconnected model";
  await emit(page, "reset");
  await expect(
    page.getByRole("dialog").getByText("Reconnected model", { exact: true }),
  ).toBeVisible();
  await page.evaluate(() =>
    Object.defineProperty(document, "hidden", {
      configurable: true,
      value: true,
    }),
  );
  mock.snapshots.get(ALPHA)!.model = "Visible model";
  await emit(page, "run.changed", ALPHA);
  await expect(
    page.getByRole("dialog").getByText("Reconnected model", { exact: true }),
  ).toBeVisible();
  await page.evaluate(() => {
    Object.defineProperty(document, "hidden", {
      configurable: true,
      value: false,
    });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect(
    page.getByRole("dialog").getByText("Visible model", { exact: true }),
  ).toBeVisible();
});

test("30 second reconciliation continues after stream errors", async ({
  page,
}) => {
  const mock = await mockApi(page);
  await page.clock.install();
  await openAlpha(page);
  await emit(page, "error");
  mock.snapshots.get(ALPHA)!.model = "Fallback model";
  await page.clock.fastForward(30_000);
  await page.clock.runFor(200);
  await expect(
    page.getByRole("dialog").getByText("Fallback model", { exact: true }),
  ).toBeVisible();
});

test("older activity and execution pages and cursors survive live refreshes", async ({
  page,
}) => {
  const mock = await mockApi(page);
  const first = { created, id: "page-one" };
  const second = { created, id: "page-two" };
  mock.activities.set(ALPHA, {
    activities: [activity("a-new", "Newest activity")],
    next_cursor: first,
  });
  mock.executions.set(ALPHA, {
    executions: [execution("e-new", "Newest execution")],
    next_cursor: first,
  });
  mock.olderActivities.set(first.id, {
    activities: [activity("a-old", "Older activity")],
    next_cursor: second,
  });
  mock.olderActivities.set(second.id, {
    activities: [activity("a-last", "Last activity")],
    next_cursor: null,
  });
  mock.olderExecutions.set(first.id, {
    executions: [execution("e-old", "Older execution")],
    next_cursor: second,
  });
  mock.olderExecutions.set(second.id, {
    executions: [execution("e-last", "Last execution")],
    next_cursor: null,
  });
  await openAlpha(page);
  await page.getByRole("tab", { name: "Activities", exact: true }).click();
  await page
    .getByRole("button", { name: "Load older activities", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Older activity", exact: true }),
  ).toBeVisible();
  await page.getByRole("tab", { name: "Executions", exact: true }).click();
  await page
    .getByRole("button", { name: "Load older executions", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Older execution", exact: true }),
  ).toBeVisible();
  mock.activities.set(ALPHA, {
    activities: [
      activity("a-live", "Fresh activity"),
      activity("a-new", "Newest activity"),
    ],
    next_cursor: first,
  });
  mock.executions.set(ALPHA, {
    executions: [
      execution("e-live", "Fresh execution"),
      execution("e-new", "Newest execution"),
    ],
    next_cursor: first,
  });
  await emit(page, "run.changed", ALPHA);
  await expect(
    page.getByRole("heading", { name: "Fresh execution", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Older execution", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Load older executions", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Last execution", exact: true }),
  ).toBeVisible();
  await page.getByRole("tab", { name: "Activities", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Fresh activity", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Older activity", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Load older activities", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Last activity", exact: true }),
  ).toBeVisible();
  for (const feed of ["activities", "executions"])
    expect(
      mock.calls
        .filter(
          (url) =>
            url.pathname.endsWith(`/${feed}`) &&
            url.searchParams.has("before_id"),
        )
        .map((url) => url.searchParams.get("before_id")),
    ).toEqual([first.id, second.id]);
});

test("closing and switching runs discards a delayed snapshot and closes its subscription", async ({
  page,
}) => {
  const mock = await mockApi(page);
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  mock.snapshots.get(ALPHA)!.model = "Stale Alpha model";
  mock.setDetailGate(async (id) => {
    if (id === ALPHA) await gate;
  });
  await page.goto("./#runs");
  await page
    .locator(".run-row")
    .filter({ hasText: "Alpha" })
    .getByRole("button", { name: "Open run" })
    .click();
  await expect.poll(() => mock.count(`/api/runs/${ALPHA}`)).toBeGreaterThan(0);
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Close", exact: true })
    .click();
  await page
    .locator(".run-row")
    .filter({ hasText: "Beta" })
    .getByRole("button", { name: "Open run" })
    .click();
  await expect(
    page.getByRole("dialog").getByText("Beta model", { exact: true }),
  ).toBeVisible();
  release();
  await emit(page, "reset", BETA);
  await expect(page.getByRole("dialog")).not.toContainText("Stale Alpha");
  const alphaSource = (await sourceCounts(page)).find((source) =>
    source.url.includes(ALPHA),
  );
  expect(alphaSource).toMatchObject({ closed: true, listeners: 0 });
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Close", exact: true })
    .click();
  const betaSources = (await sourceCounts(page)).filter((source) =>
    source.url.includes(BETA),
  );
  expect(betaSources.length).toBeGreaterThan(0);
  expect(
    betaSources.every((source) => source.closed && source.listeners === 0),
  ).toBe(true);
});

test("global hints preserve run pagination and auto-refresh controls the subscription", async ({
  page,
}) => {
  const mock = await mockApi(page);
  mock.list.first = [ALPHA];
  mock.list.older = [BETA];
  mock.list.next = 25;
  mock.list.olderNext = 50;
  await page.goto("./#runs");
  await page
    .getByRole("button", { name: "Load more runs", exact: true })
    .click();
  await expect(
    page.locator(".run-row").filter({ hasText: "Beta" }),
  ).toBeVisible();
  mock.snapshots.get(ALPHA)!.model = "Newest list model";
  await emit(page, "run.changed", ALPHA);
  await expect(
    page.locator(".run-row").filter({ hasText: "Alpha" }),
  ).toContainText("Newest list model");
  await expect(
    page.locator(".run-row").filter({ hasText: "Beta" }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Load more runs", exact: true })
    .click();
  await expect
    .poll(() =>
      mock.calls
        .filter((url) => url.pathname === "/api/runs")
        .at(-1)
        ?.searchParams.get("offset"),
    )
    .toBe("50");
  await page
    .getByRole("switch", { name: "Auto-refresh data every 30 seconds" })
    .click();
  expect(
    (await sourceCounts(page)).every(
      (source) => source.closed && source.listeners === 0,
    ),
  ).toBe(true);
  const reads = mock.count("/api/runs");
  await emit(page, "reset");
  expect(mock.count("/api/runs")).toBe(reads);
  await page
    .getByRole("switch", { name: "Auto-refresh data every 30 seconds" })
    .click();
  await emit(page, "reset");
  await expect.poll(() => mock.count("/api/runs")).toBeGreaterThan(reads);
});

test("workspace run hints update the selected workspace without reloading handoffs", async ({
  page,
}) => {
  const mock = await mockApi(page);
  await page.goto("./#handoffs");
  await page.getByLabel("Workspace", { exact: true }).selectOption(WORKSPACE);
  await expect(
    page.locator(".run-row").filter({ hasText: "Alpha" }),
  ).toBeVisible();
  const handoffReads = mock.count(`/api/workspaces/${WORKSPACE}/jobs`);
  const diagnostics = mock.count("/api/diagnostics");
  mock.snapshots.get(ALPHA)!.model = "Workspace live model";
  await emit(page, "run.changed", ALPHA);
  await expect(
    page.locator(".run-row").filter({ hasText: "Alpha" }),
  ).toContainText("Workspace live model");
  expect(mock.count(`/api/workspaces/${WORKSPACE}/jobs`)).toBe(handoffReads);
  expect(mock.count("/api/diagnostics")).toBe(diagnostics);
});

test("a disjoint full feed page resets its cursor so reconnect bursts leave no gap", async ({
  page,
}) => {
  const mock = await mockApi(page);
  const first = { created, id: "initial-page" };
  const oldCursor = { created, id: "old-history-page" };
  const burstCursor = { created, id: "burst-page" };
  mock.activities.set(ALPHA, {
    activities: [activity("original-a", "Original activity")],
    next_cursor: first,
  });
  mock.executions.set(ALPHA, {
    executions: [execution("original-e", "Original execution")],
    next_cursor: first,
  });
  mock.olderActivities.set(first.id, {
    activities: [activity("history-a", "History activity")],
    next_cursor: oldCursor,
  });
  mock.olderExecutions.set(first.id, {
    executions: [execution("history-e", "History execution")],
    next_cursor: oldCursor,
  });
  await openAlpha(page);
  await page.getByRole("tab", { name: "Activities", exact: true }).click();
  await page
    .getByRole("button", { name: "Load older activities", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "History activity", exact: true }),
  ).toBeVisible();
  await page.getByRole("tab", { name: "Executions", exact: true }).click();
  await page
    .getByRole("button", { name: "Load older executions", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "History execution", exact: true }),
  ).toBeVisible();
  mock.activities.set(ALPHA, {
    activities: Array.from({ length: 50 }, (_, index) =>
      activity(`burst-a-${index}`, `Burst activity ${index}`),
    ),
    next_cursor: burstCursor,
  });
  mock.executions.set(ALPHA, {
    executions: Array.from({ length: 50 }, (_, index) =>
      execution(`burst-e-${index}`, `Burst execution ${index}`),
    ),
    next_cursor: burstCursor,
  });
  mock.olderActivities.set(burstCursor.id, {
    activities: [activity("missing-a", "Gap activity")],
    next_cursor: null,
  });
  mock.olderExecutions.set(burstCursor.id, {
    executions: [execution("missing-e", "Gap execution")],
    next_cursor: null,
  });
  await emit(page, "reset");
  await expect(
    page.getByRole("heading", { name: "Burst execution 0", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "History execution", exact: true }),
  ).toHaveCount(0);
  await page
    .getByRole("button", { name: "Load older executions", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Gap execution", exact: true }),
  ).toBeVisible();
  await page.getByRole("tab", { name: "Activities", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Burst activity 0", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "History activity", exact: true }),
  ).toHaveCount(0);
  await page
    .getByRole("button", { name: "Load older activities", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Gap activity", exact: true }),
  ).toBeVisible();
  expect(
    mock.calls.some(
      (url) => url.searchParams.get("before_id") === oldCursor.id,
    ),
  ).toBe(false);
  expect(
    mock.calls.filter(
      (url) => url.searchParams.get("before_id") === burstCursor.id,
    ),
  ).toHaveLength(2);
});

test("a full page of new runs restarts list pagination and preserves pages on reconciliation", async ({
  page,
}) => {
  const mock = await mockApi(page);
  mock.list.first = [ALPHA];
  mock.list.older = [BETA];
  mock.list.next = 25;
  mock.list.olderNext = 50;
  await page.clock.install();
  await page.goto("./#runs");
  await page
    .getByRole("button", { name: "Load more runs", exact: true })
    .click();
  await expect(
    page.locator(".run-row").filter({ hasText: "Beta" }),
  ).toBeVisible();
  await page.clock.fastForward(30_000);
  await expect(
    page.locator(".run-row").filter({ hasText: "Beta" }),
  ).toBeVisible();
  mock.list.first = Array.from({ length: 25 }, (_, index) => {
    const id = `run_${String(index).padStart(24, "0")}`;
    mock.snapshots.set(id, run(id, `New run ${index}`));
    return id;
  });
  mock.snapshots.set(
    "run_cccccccccccccccccccccccc",
    run("run_cccccccccccccccccccccccc", "Gap run"),
  );
  mock.list.older = ["run_cccccccccccccccccccccccc"];
  await emit(page, "reset");
  await page.clock.runFor(200);
  await expect(
    page.locator(".run-row").filter({ hasText: "New run 0" }),
  ).toBeVisible();
  await expect(
    page.locator(".run-row").filter({ hasText: "Beta" }),
  ).toHaveCount(0);
  await page
    .getByRole("button", { name: "Load more runs", exact: true })
    .click();
  await expect(
    page.locator(".run-row").filter({ hasText: "Gap run" }),
  ).toBeVisible();
  expect(
    mock.calls
      .filter((url) => url.pathname === "/api/runs")
      .at(-1)
      ?.searchParams.get("offset"),
  ).toBe("25");
});
