// 3B1 spawn contract: read-only argv when disabled, writable argv plus
// exactly one package-owned trusted extension when enabled, no bash, and
// fail-closed creation when the extension is missing.
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

test("read-only argv is unchanged and shell-free", () => {
  assert.deepEqual(piRpcArgv(),
    ["--mode", "rpc", "--tools", "read,grep,find,ls", "--no-approve", "--no-extensions"]);
  assert.deepEqual(piRpcArgvFor({ writable: false }), piRpcArgv());
  assert.deepEqual(piRpcArgvFor(), piRpcArgv());
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
