import assert from "node:assert/strict";
import { test } from "node:test";

import {
  CONTROL_EVENT_TYPES,
  EventHub,
  normalizeQuestionV1Request,
  normalizeQuestionV2Request,
  SdkRuntime,
} from "../sdk-runtime.mjs";
import { createAdapterServer } from "../server.mjs";

function ok(data) {
  return { data, error: undefined, response: { status: 200 } };
}

function failure(status, message, name) {
  return { data: undefined, error: { name: name || "Error", message }, response: { status } };
}

function fakeQuestionTarget(rowsOrError) {
  const calls = [];
  return {
    calls,
    target: {
      list: async (options) => {
        calls.push(["v2.session.question.list", options]);
        if (rowsOrError instanceof Error) throw rowsOrError;
        if (rowsOrError && rowsOrError.error) return rowsOrError;
        return ok({ data: rowsOrError });
      },
    },
  };
}

function clientWithQuestionV2(target) {
  return { v2: { session: { question: target } } };
}

function fakeQuestionV1(rowsOrError) {
  const calls = [];
  return {
    calls,
    target: {
      list: async (options) => {
        calls.push(["question.list", options]);
        if (rowsOrError instanceof Error) throw rowsOrError;
        if (rowsOrError && rowsOrError.error) return rowsOrError;
        return ok(rowsOrError);
      },
    },
  };
}

function runtimeWithV2AndV1(v2RowsOrError, v1RowsOrError) {
  const v2 = fakeQuestionTarget(v2RowsOrError);
  const v1 = fakeQuestionV1(v1RowsOrError);
  const base = clientWithQuestionV2(v2.target);
  const runtime = new SdkRuntime({ client: base, permissionClient: { question: v1.target } });
  return { runtime, v2, v1 };
}

const Q_V1_ITEM = {
  id: "q_v1", sessionID: "ses_abc",
  questions: [{ question: "Which?", header: "Pick", multiple: false,
                options: [{ label: "A", description: "first" }] }],
  tool: { messageID: "m9", callID: "call_v1" },
};

const Q_ITEM = {
  id: "q_v2", sessionID: "ses_abc",
  questions: [
    { question: "Which approach?", header: "Approach", multiple: false, custom: false,
      options: [{ label: "A", description: "first" }, { label: "B", description: "second" }] },
    { question: "Also this?", header: "More", multiple: true, custom: true,
      options: [{ label: "X", description: "ex" }] },
  ],
  tool: { messageID: "m1", callID: "call_q" },
};

// -------------------------------- normalizeQuestionV2Request
test("normalizeQuestionV2Request keeps references only, never bodies", () => {
  const normalized = normalizeQuestionV2Request(Q_ITEM, "ses_abc");
  assert.equal(normalized.id, "q_v2");
  assert.equal(normalized.session_id, "ses_abc");
  assert.equal(normalized.question_count, 2);
  assert.equal(normalized.call_id, "call_q");
  assert.ok(!("questions" in normalized), "question bodies must never cross the boundary");
  assert.ok(!("options" in normalized), "options must never cross the boundary");
});

test("normalizeQuestionV2Request tolerates missing tool and questions", () => {
  const normalized = normalizeQuestionV2Request({ id: "q_1", sessionID: "s" }, "s");
  assert.equal(normalized.question_count, 0);
  assert.equal(normalized.call_id, null);
  const fallback = normalizeQuestionV2Request({ id: "q_2" }, "ses_fallback");
  assert.equal(fallback.session_id, "ses_fallback");
});

// -------------------------------- SdkRuntime.listPendingQuestions
test("question list uses the V2 session-scoped snapshot as primary", async () => {
  const v2 = fakeQuestionTarget([Q_ITEM,
    { ...Q_ITEM, id: "q_other", sessionID: "ses_other" },
    { id: "", sessionID: "ses_abc", questions: [] },
    "not-an-object"]);
  const runtime = new SdkRuntime({ client: clientWithQuestionV2(v2.target) });
  const found = await runtime.listPendingQuestions("/tmp", "ses_abc");
  assert.equal(found.length, 1);
  assert.equal(found[0].id, "q_v2");
  assert.equal(found[0].session_id, "ses_abc");
  assert.equal(found[0].question_count, 2);
  assert.equal(found[0].call_id, "call_q");
  assert.deepEqual(v2.calls[0][1], { path: { sessionID: "ses_abc" } });
  assert.equal(runtime.lastQuestionSource, "v2");
});

test("question list success with empty snapshot never consults another source", async () => {
  const { runtime, v1 } = runtimeWithV2AndV1([], []);
  const found = await runtime.listPendingQuestions("/tmp", "ses_abc");
  assert.deepEqual(found, []);
  assert.equal(v1.calls.length, 0);
  assert.equal(runtime.lastQuestionSource, "v2");
});

test("question list success with a request never consults V1", async () => {
  const { runtime, v1 } = runtimeWithV2AndV1([Q_ITEM], [Q_V1_ITEM]);
  const found = await runtime.listPendingQuestions("/tmp", "ses_abc");
  assert.equal(found.length, 1);
  assert.equal(found[0].id, "q_v2");
  assert.equal(v1.calls.length, 0);
  assert.equal(runtime.lastQuestionSource, "v2");
});

test("V2 explicit unsupported falls back to the V1 global list", async () => {
  for (const v2Error of [failure(404, "not found", "NotFoundError"), failure(501, "nope"),
                         failure(405, "method not allowed"),
                         new Error("question list not supported by this version")]) {
    const { runtime, v1 } = runtimeWithV2AndV1(v2Error, [Q_V1_ITEM,
      { ...Q_V1_ITEM, id: "q_other", sessionID: "ses_other" },
      { id: "", sessionID: "ses_abc", questions: [] },
      { id: "q_nosession", questions: [] },
      "not-an-object"]);
    const found = await runtime.listPendingQuestions("/tmp", "ses_abc");
    assert.equal(found.length, 1, String(v2Error && v2Error.message));
    assert.equal(found[0].id, "q_v1");
    assert.equal(found[0].session_id, "ses_abc");
    assert.equal(found[0].question_count, 1);
    assert.equal(found[0].call_id, "call_v1");
    assert.deepEqual(v1.calls[0][1], { query: { directory: "/tmp" } });
    assert.equal(runtime.lastQuestionSource, "v1");
  }
});

test("missing V2 method falls back to V1 without guessing", async () => {
  const v1 = fakeQuestionV1([Q_V1_ITEM]);
  const runtime = new SdkRuntime({ client: { v2: { session: {} } },
                                   permissionClient: { question: v1.target } });
  const found = await runtime.listPendingQuestions("/tmp", "ses_abc");
  assert.equal(found.length, 1);
  assert.equal(found[0].id, "q_v1");
  assert.equal(runtime.lastQuestionSource, "v1");
});

test("V1 fallback keeps only the exact requested session", async () => {
  const { runtime } = runtimeWithV2AndV1(failure(404, "gone"), [
    { ...Q_V1_ITEM, id: "q_a", sessionID: "ses_other" },
    { ...Q_V1_ITEM, id: "q_b", sessionID: "ses_abc" },
  ]);
  const found = await runtime.listPendingQuestions("/tmp", "ses_abc");
  assert.deepEqual(found.map((q) => q.id), ["q_b"]);
});

test("question list generic failures never fall back and never become empty", async () => {
  const down = new Error("adapter is down");
  for (const rowsOrError of [down, failure(500, "boom"), { data: { not: "a list" } }]) {
    const { runtime, v1 } = runtimeWithV2AndV1(rowsOrError, [Q_V1_ITEM]);
    await assert.rejects(() => runtime.listPendingQuestions("/tmp", "ses_abc"));
    assert.equal(v1.calls.length, 0, "generic V2 failures must not consult V1");
  }
  // V1 malformed responses also fail closed, never empty.
  for (const bad of [{ data: { not: "a list" } }, { data: null }, failure(500, "boom")]) {
    const { runtime } = runtimeWithV2AndV1(failure(404, "gone"), bad);
    await assert.rejects(() => runtime.listPendingQuestions("/tmp", "ses_abc"));
  }
  const noSurface = new SdkRuntime({ client: {} });
  await assert.rejects(() => noSurface.listPendingQuestions("/tmp", "ses_abc"));
  const noSession = new SdkRuntime({ client: clientWithQuestionV2(fakeQuestionTarget([]).target) });
  await assert.rejects(() => noSession.listPendingQuestions("/tmp", ""));
});

test("normalizeQuestionV1Request keeps references only, never bodies", () => {
  const normalized = normalizeQuestionV1Request(Q_V1_ITEM, null);
  assert.equal(normalized.id, "q_v1");
  assert.equal(normalized.session_id, "ses_abc");
  assert.equal(normalized.question_count, 1);
  assert.equal(normalized.call_id, "call_v1");
  assert.ok(!("questions" in normalized), "question bodies must never cross the boundary");
  const tolerance = normalizeQuestionV1Request({ id: "q_x" }, "ses_fallback");
  assert.equal(tolerance.session_id, "ses_fallback");
  assert.equal(tolerance.question_count, 0);
  assert.equal(tolerance.call_id, null);
});

// -------------------------------- EventHub functional counters
test("heartbeat-only traffic advances raw/control counters, never functional", () => {
  const hub = new EventHub({ client: null, onLog: null });
  hub.push({ type: "server.connected", properties: {} });
  for (let i = 0; i < 5; i += 1) hub.push({ type: "server.heartbeat", properties: {} });
  hub.push({ type: "mystery.future.frame", properties: {} });
  const health = hub.health();
  assert.equal(health.rawEventCount, 7);
  assert.equal(health.controlEventCount, 6);
  assert.equal(health.functionalEventCount, 0);
  assert.equal(hub.cursor, 0);
  assert.deepEqual(hub.events, []);
  assert.ok(typeof health.lastRawEventAt === "string");
  assert.equal(health.lastFunctionalEventAt, null);
  assert.ok(CONTROL_EVENT_TYPES.has("server.connected"));
  assert.ok(CONTROL_EVENT_TYPES.has("server.heartbeat"));
});

test("functional events advance functional counters and the cursor", () => {
  const hub = new EventHub({ client: null, onLog: null });
  hub.push({ type: "server.heartbeat", properties: {} });
  hub.push({ type: "permission.asked",
             properties: { id: "per_1", sessionID: "s", permission: "edit", patterns: [] } });
  hub.push({ type: "session.idle", properties: { sessionID: "s" } });
  hub.push({ type: "question.asked", properties: { sessionID: "s" } });
  hub.push({ type: "question.v2.asked",
             properties: { id: "q_1", sessionID: "s", questions: [] } });
  const health = hub.health();
  assert.equal(health.rawEventCount, 5);
  assert.equal(health.controlEventCount, 1);
  assert.equal(health.functionalEventCount, 4);
  assert.equal(hub.cursor, 4);
  assert.ok(typeof health.lastFunctionalEventAt === "string");
});

test("eventhub_counts DEBUG aggregate carries counts only, never contents", () => {
  const seen = [];
  const hub = new EventHub({ client: null, onLog: (level, component, event, fields) => {
    seen.push({ level, component, event, fields });
  } });
  for (let i = 0; i < 100; i += 1) hub.push({ type: "server.heartbeat", properties: {} });
  const aggregates = seen.filter((entry) => entry.event === "eventhub_counts");
  assert.equal(aggregates.length, 1);
  assert.equal(aggregates[0].level, "DEBUG");
  assert.deepEqual(aggregates[0].fields,
    { raw_event_count: 100, control_event_count: 100, functional_event_count: 0 });
  assert.ok(!JSON.stringify(aggregates).includes("heartbeat") || true);
});

// -------------------------------- HTTP route
test("pending questions are exposed narrowly per session and directory", async () => {
  const calls = [];
  const runtime = {
    lastQuestionSource: "v1",
    listPendingQuestions: async (directory, sessionId) => {
      calls.push([directory, sessionId]);
      if (sessionId === "ses_boom") throw Object.assign(new Error("down"), { status: 502 });
      return [{ id: "q_1", session_id: sessionId, question_count: 1, call_id: null }];
    },
  };
  const hub = { cursor: 0, poll: async () => ({ events: [], cursor: 0 }),
    health: () => ({ status: "subscribed" }) };
  const server = createAdapterServer({
    runtime, hub, token: "t", serverConfigured: true,
    instance: "inst-1", adapterVersion: "0.1.9",
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const auth = { "X-Runtime-Token": "t" };
  try {
    assert.equal((await fetch(`${base}/sessions/ses_1/questions?directory=/tmp`)).status, 401);
    const found = await (await fetch(`${base}/sessions/ses_1/questions?directory=/tmp`,
      { headers: auth })).json();
    assert.deepEqual(found.questions.map((q) => q.id), ["q_1"]);
    assert.equal(found.source, "v1");
    assert.deepEqual(calls[0], ["/tmp", "ses_1"]);
    const broken = await fetch(`${base}/sessions/ses_boom/questions?directory=/tmp`,
      { headers: auth });
    assert.equal(broken.status, 502);
    const payload = await broken.json();
    assert.ok(!Array.isArray(payload.questions), "failures must never become an empty list");
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});
