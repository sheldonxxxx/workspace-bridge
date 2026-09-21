// 3C1 spawn contract: read-only AND writable managed sessions carry
// exactly one package-owned trusted extension (read policy is enforced in
// both modes; write_tools_enabled exposes edit/write; shell_mode exposes
// bash only when != deny, no powershell, no command rules). piRpcArgv()
// stays as the legacy no-extension read-only argv for the legacy
// no-policy path.
import assert from "node:assert/strict";
import { test } from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import {
  isTrustedExtensionUsable,
  piRpcArgv,
  piRpcArgvFor,
  trustedExtensionPath,
} from "../config.mjs";

test("legacy read-only argv is unchanged and shell-free", () => {
  assert.deepEqual(piRpcArgv(),
    ["--mode", "rpc", "--tools", "read,grep,find,ls", "--no-approve", "--no-extensions"]);
});

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

test("read-only managed argv carries the trusted extension but no edit/write/bash", () => {
  const argv = piRpcArgvFor({ writable: false });
  assert.deepEqual(argv.slice(0, 5),
    ["--mode", "rpc", "--tools", "read,grep,find,ls", "--no-approve"]);
  assert.ok(argv.includes("--no-extensions"));
  const flags = argv.filter((a) => a === "-e");
  assert.equal(flags.length, 1);
  assert.equal(argv[argv.indexOf("-e") + 1], trustedExtensionPath());
  assert.ok(!argv.join(" ").includes("bash"));
  assert.ok(!argv.join(",").includes("edit"));
});

test("writable argv carries edit/write plus exactly one trusted extension", () => {
  const argv = piRpcArgvFor({ writable: true });
  assert.deepEqual(argv.slice(0, 5),
    ["--mode", "rpc", "--tools", "read,grep,find,ls,edit,write", "--no-approve"]);
  assert.ok(argv.includes("--no-extensions"));
  const flags = argv.filter((a) => a === "-e");
  assert.equal(flags.length, 1);
  assert.equal(argv[argv.indexOf("-e") + 1], trustedExtensionPath());
  assert.ok(!argv.join(" ").includes("bash"));
});

test("shell argv adds bash only when shell_mode != deny (no powershell, no rules)", () => {
  const deny = piRpcArgvFor({ writable: true, shellMode: "deny" });
  assert.ok(!deny.join(",").split(",").includes("bash"));
  for (const mode of ["ask", "allow"]) {
    const argv = piRpcArgvFor({ writable: false, shellMode: mode });
    assert.ok(argv.join(",").split(",").includes("bash"));
    assert.ok(!argv.join(" ").includes("powershell"));
  }
  const tools = piRpcArgvFor({ writable: true, shellMode: "allow" })[3];
  assert.ok(tools.includes("bash"));
  assert.ok(!tools.includes("powershell"));
});
