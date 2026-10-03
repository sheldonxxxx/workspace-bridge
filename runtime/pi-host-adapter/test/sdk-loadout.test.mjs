// 3C1 SDK tool loadout: read-only AND writable managed sessions carry
// the exact built-in allowlist plus the package-owned trusted permission
// extension (read policy is enforced in both modes;
// write_tools_enabled exposes edit/write; shell_mode exposes bash only
// when != deny, no powershell, no command rules). With enabled
// third-party extensions no allowlist is passed (it would hide extension
// tools) and built-in exposure uses excludeTools instead.
import assert from "node:assert/strict";
import { test } from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import {
  isTrustedExtensionUsable,
  sdkExcludeTools,
  sdkToolLoadout,
  trustedExtensionPath,
} from "../config.mjs";

test("trusted extension is a package-owned regular file", () => {
  const target = trustedExtensionPath();
  assert.ok(path.isAbsolute(target));
  assert.ok(target.endsWith("trusted-permission-extension.mjs"));
  assert.equal(isTrustedExtensionUsable(), true);
  assert.equal(isTrustedExtensionUsable(target), true);
  assert.equal(isTrustedExtensionUsable(path.join(os.tmpdir(), "pi-no-such-extension.mjs")), false);
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pi-ext-dir-"));
  assert.equal(isTrustedExtensionUsable(dir), false);
});

test("read-only loadout allows the read family without edit/write/bash", () => {
  const loadout = sdkToolLoadout({ writable: false });
  assert.deepEqual(loadout.tools, ["read", "grep", "find", "ls"]);
  assert.equal(loadout.excludeTools, null);
  assert.deepEqual(loadout.extensionPaths, []);
});

test("writable loadout adds edit/write, still no bash", () => {
  const loadout = sdkToolLoadout({ writable: true });
  assert.deepEqual(loadout.tools, ["read", "grep", "find", "ls", "edit", "write"]);
  assert.equal(loadout.excludeTools, null);
});

test("shell loadout adds bash only when shell_mode != deny (no powershell)", () => {
  const deny = sdkToolLoadout({ writable: true, shellMode: "deny" });
  assert.ok(!deny.tools.includes("bash"));
  for (const mode of ["ask", "allow"]) {
    const loadout = sdkToolLoadout({ writable: false, shellMode: mode });
    assert.ok(loadout.tools.includes("bash"));
    assert.ok(!loadout.tools.includes("powershell"));
  }
});

test("extensions-enabled loadout uses excludeTools, never an allowlist", () => {
  const loadout = sdkToolLoadout({
    writable: false, shellMode: "deny", extensionRoots: ["/pkg/alpha"],
  });
  assert.equal(loadout.tools, null);
  assert.ok(loadout.excludeTools.includes("edit"));
  assert.ok(loadout.excludeTools.includes("bash"));
  assert.ok(loadout.excludeTools.includes("powershell"));
  assert.deepEqual(loadout.extensionPaths, ["/pkg/alpha"]);

  const writable = sdkToolLoadout({
    writable: true, shellMode: "allow", extensionRoots: ["/pkg/alpha"],
  });
  assert.equal(writable.tools, null);
  const hidden = writable.excludeTools.split(",");
  assert.ok(!hidden.includes("edit"));
  assert.ok(!hidden.includes("bash"));
  assert.ok(hidden.includes("powershell"));
});

test("sdkExcludeTools hides edit/write when read-only and bash when denied", () => {
  const excludes = sdkExcludeTools({ writable: false, shellMode: "deny" });
  assert.ok(excludes.includes("edit") && excludes.includes("bash") && excludes.includes("powershell"));
  const writable = sdkExcludeTools({ writable: true, shellMode: "allow" });
  assert.ok(!writable.split(",").includes("edit"));
  assert.ok(!writable.split(",").includes("bash"));
  assert.ok(writable.split(",").includes("powershell"));
});

