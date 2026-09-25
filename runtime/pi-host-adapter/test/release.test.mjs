import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { computeBuildId, piRelease, readPackageMeta, validateRelease } from "../release.mjs";
import { PiRuntimeProtocol } from "../wbrp.mjs";

const ROOT = path.resolve(import.meta.dirname, "..");

test("Pi build ID is deterministic and bounded", () => {
  const first = computeBuildId(ROOT);
  const second = computeBuildId(ROOT);
  assert.equal(first, second);
  assert.match(first, /^sha256:[0-9a-f]{64}$/);
});

test("Pi release carries WB product version without conflating adapter version", () => {
  const meta = readPackageMeta(ROOT);
  assert.equal(meta.workspaceBridgeRelease, "0.8.4");
  assert.equal(meta.version, "0.4.0");
  const release = piRelease(ROOT);
  assert.equal(release.contract, 1);
  assert.equal(release.product, "workspace-bridge");
  assert.equal(release.product_version, "0.8.4");
  assert.equal(release.component, "pi-host-adapter");
  assert.equal(release.component_version, "0.4.0");
  assert.match(release.build_id, /^sha256:[0-9a-f]{64}$/);
  assert.deepEqual(validateRelease(release), release);
  // No paths, hostnames, tokens, or instance IDs in the public identity.
  const text = JSON.stringify(release);
  assert.ok(!text.includes(ROOT));
  assert.ok(!text.includes("127.0.0.1"));
});

test("Pi descriptor publishes the release identity additively", () => {
  const agentDir = fs.mkdtempSync(path.join(os.tmpdir(), "bridge-pi-release-"));
  try {
    const adapter = {
      agentDir,
      piVersion: "test",
      sessions: new Map(),
      listModelsFn: async () => [],
    };
    const protocol = new PiRuntimeProtocol(adapter);
    const descriptor = protocol.descriptor();
    assert.equal(descriptor.runtime.id, "pi");
    assert.equal(descriptor.runtime.adapterVersion, "1.0.0");
    assert.ok(descriptor.release);
    assert.equal(descriptor.release.component, "pi-host-adapter");
    assert.equal(descriptor.release.product_version, "0.8.4");
  } finally {
    fs.rmSync(agentDir, { recursive: true, force: true });
  }
});

test("Pi production inputs exclude generated and test artifacts", async () => {
  const { listProductionInputs } = await import("../release.mjs");
  const names = listProductionInputs(ROOT);
  assert.ok(names.includes("package.json"));
  assert.ok(names.includes("package-lock.json"));
  assert.ok(names.includes("wbrp.mjs"));
  assert.ok(names.includes("release.mjs"));
  assert.ok(!names.some((n) => n.startsWith("test/")));
  assert.ok(!names.some((n) => n.startsWith("launchd/")));
  assert.ok(!names.includes("README.md"));
  assert.ok(!names.some((n) => n.includes("node_modules")));
  assert.deepEqual([...names].sort(), names);
});
