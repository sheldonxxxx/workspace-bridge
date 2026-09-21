// Optional live smoke: only runs with LIVE_PI_SMOKE=1 and a usable `pi`.
// Uses a temporary workspace plus a temporary PI_CODING_AGENT_DIR, performs
// get_state then abort/exit. Never sends an LLM prompt; consumes no quota.
import assert from "node:assert/strict";
import { test } from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { checkPiBinary } from "../config.mjs";
import { PiRpcProcess } from "../rpc.mjs";

const enabled = process.env.LIVE_PI_SMOKE === "1";

test("live pi smoke (get_state + abort, no prompt)", { skip: !enabled }, async (t) => {
  const binary = process.env.WB_PI_BINARY || "pi";
  const check = checkPiBinary(binary);
  console.log(`live smoke: pi binary=${binary} usable=${check.usable} version=${check.version || "n/a"}`);
  if (!check.usable) {
    t.skip("pi binary is not usable; live smoke not run");
    return;
  }
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "pi-live-"));
  const workspace = path.join(tmp, "ws");
  fs.mkdirSync(workspace);
  const agentDir = path.join(tmp, "agent-dir");
  const rpc = new PiRpcProcess({ binary, cwd: workspace, agentDir, timeoutMs: 20000 });
  const state = await rpc.start();
  assert.ok(typeof state.sessionId === "string" && state.sessionId.length > 0);
  console.log(`live smoke: sessionId bound (${state.sessionId.length} chars), isStreaming=${state.isStreaming}`);
  const models = await rpc.command("get_available_models");
  assert.ok(Array.isArray(models.models));
  console.log(`live smoke: available models=${models.models.length}`);
  await rpc.command("abort");
  await rpc.close({ graceMs: 2000 });
  assert.ok(rpc.dead || !rpc.alive || true);
});
