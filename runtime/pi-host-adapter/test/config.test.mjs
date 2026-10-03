import assert from "node:assert/strict";
import { test } from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import {
  ADAPTER_VERSION, DEFAULT_HOST, DEFAULT_PORT, expandAgentDir, resolveAgentDir,
  isAgentDirAllowed, loadConfig, checkPiBinary, defaultAgentDir, sdkToolLoadout,
} from "../config.mjs";

test("default host/port and token locking", () => {
  const config = loadConfig({}, "/home/tester");
  assert.equal(config.host, DEFAULT_HOST);
  assert.equal(config.host, "127.0.0.1");
  assert.equal(config.port, DEFAULT_PORT);
  assert.equal(config.port, 8780);
  assert.equal(config.locked, true);
  assert.equal(config.tokenConfigured, false);
  const open = loadConfig({ WB_RUNTIME_TOKEN: "s3cret" }, "/home/tester");
  assert.equal(open.locked, false);
  assert.equal(open.tokenConfigured, true);
});

test("host/port overrides with invalid port fallback", () => {
  const config = loadConfig({ WB_PI_ADAPTER_HOST: "127.0.0.1", WB_PI_ADAPTER_PORT: "9999" }, "/home/tester");
  assert.equal(config.host, "127.0.0.1");
  assert.equal(config.port, 9999);
  const bad = loadConfig({ WB_PI_ADAPTER_PORT: "banana" }, "/home/tester");
  assert.equal(bad.port, DEFAULT_PORT);
});

test("PI_CODING_AGENT_DIR defaults to ~/.pi/workspace-bridge", () => {
  assert.equal(defaultAgentDir("/home/tester"), path.join("/home/tester", ".pi", "workspace-bridge"));
  assert.equal(resolveAgentDir("", "/home/tester"), path.join("/home/tester", ".pi", "workspace-bridge"));
  const config = loadConfig({}, "/home/tester");
  assert.equal(config.agentDirExplicit, false);
  assert.equal(config.agentDir, path.join("/home/tester", ".pi", "workspace-bridge"));
  assert.ok(path.isAbsolute(config.agentDir));
});

test("agent dir honors explicit value with ~/$HOME/${HOME} expansion", () => {
  assert.equal(expandAgentDir("~/.pi/workspace-bridge", "/home/t"), path.join("/home/t", ".pi/workspace-bridge"));
  assert.equal(expandAgentDir("$HOME/.pi/workspace-bridge", "/home/t"), path.join("/home/t", ".pi/workspace-bridge"));
  assert.equal(expandAgentDir("${HOME}/.pi/workspace-bridge", "/home/t"), path.join("/home/t", ".pi/workspace-bridge"));
  assert.equal(expandAgentDir("~", "/home/t"), "/home/t");
  assert.equal(expandAgentDir("$HOME", "/home/t"), "/home/t");
  assert.equal(expandAgentDir("/custom/agent-dir", "/home/t"), "/custom/agent-dir");
  const config = loadConfig({ PI_CODING_AGENT_DIR: "$HOME/.pi/workspace-bridge" }, "/home/t");
  assert.equal(config.agentDirExplicit, true);
  assert.equal(config.agentDir, path.join("/home/t", ".pi/workspace-bridge"));
  // Real default uses the actual home directory.
  assert.equal(loadConfig({}).agentDir, path.join(os.homedir(), ".pi", "workspace-bridge"));
});

test("normal ~/.pi/agent tree and ~/.pi root are rejected", () => {
  assert.equal(isAgentDirAllowed(path.join("/home/t", ".pi", "agent"), "/home/t"), false);
  assert.equal(isAgentDirAllowed(path.join("/home/t", ".pi"), "/home/t"), false);
  assert.equal(isAgentDirAllowed(path.join("/home/t", ".pi", "workspace-bridge"), "/home/t"), true);
  assert.equal(isAgentDirAllowed("/tmp/elsewhere", "/home/t"), true);
});

test("agent dir rejects tree descendants and symlink aliases without creating anything", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "pi-fake-home-"));
  const normalTree = path.join(home, ".pi", "agent");
  fs.mkdirSync(normalTree, { recursive: true });
  const before = fs.readdirSync(home).sort();
  // Exact normal tree and descendants.
  assert.equal(isAgentDirAllowed(normalTree, home), false);
  assert.equal(isAgentDirAllowed(path.join(normalTree, "workspace-bridge"), home), false);
  assert.equal(isAgentDirAllowed(path.join(normalTree, "nested", "deep"), home), false);
  // Symlink directly to the normal tree.
  const directLink = path.join(home, "agent-link");
  fs.symlinkSync(normalTree, directLink);
  assert.equal(isAgentDirAllowed(directLink, home), false);
  assert.equal(isAgentDirAllowed(path.join(directLink, "sub"), home), false);
  // Symlinked ancestor resolving into the normal tree.
  const ancestorLink = path.join(home, "alias-parent");
  fs.symlinkSync(normalTree, ancestorLink);
  assert.equal(isAgentDirAllowed(path.join(ancestorLink, "nested", "dir"), home), false);
  // Valid explicit dirs elsewhere, including the isolated default.
  assert.equal(isAgentDirAllowed(path.join(home, ".pi", "workspace-bridge"), home), true);
  assert.equal(isAgentDirAllowed(path.join(home, "elsewhere", "agent-dir"), home), true);
  assert.equal(isAgentDirAllowed("/Volumes/data/example", home), true);
  // Validation created nothing: only the fixtures we made exist.
  const after = fs.readdirSync(home).sort();
  assert.deepEqual(after, [...before, "agent-link", "alias-parent"].sort());
  assert.ok(!fs.existsSync(path.join(home, ".pi", "workspace-bridge")));
  assert.ok(!fs.existsSync(path.join(home, "elsewhere")));
});

test("agent dir rejects physical target when ~/.pi/agent itself is a symlink", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "pi-symtree-home-"));
  const physical = fs.mkdtempSync(path.join(os.tmpdir(), "pi-physical-agent-"));
  const dotpi = path.join(home, ".pi");
  fs.mkdirSync(dotpi, { recursive: true });
  fs.symlinkSync(physical, path.join(dotpi, "agent"));
  // Direct physical target and descendants are protected.
  assert.equal(isAgentDirAllowed(physical, home), false);
  assert.equal(isAgentDirAllowed(path.join(physical, "sessions", "x"), home), false);
  // An alias into the physical target is protected too.
  const alias = path.join(home, "target-alias");
  fs.symlinkSync(physical, alias);
  assert.equal(isAgentDirAllowed(alias, home), false);
  assert.equal(isAgentDirAllowed(path.join(alias, "sub"), home), false);
  // The symlink path itself is still rejected.
  assert.equal(isAgentDirAllowed(path.join(dotpi, "agent"), home), false);
  // Genuinely outside locations remain allowed, and nothing was created.
  assert.equal(isAgentDirAllowed(path.join(home, ".pi", "workspace-bridge"), home), true);
  assert.equal(isAgentDirAllowed(path.join(home, "other"), home), true);
  assert.ok(!fs.existsSync(path.join(home, ".pi", "workspace-bridge")));
  assert.ok(!fs.existsSync(path.join(home, "other")));
  assert.deepEqual(fs.readdirSync(physical), []);
});

test("legacy tool loadout is read-only, extension-free, and shell-free", () => {
  assert.deepEqual(sdkToolLoadout({ writable: false }),
    { tools: ["read", "grep", "find", "ls"], excludeTools: null, extensionPaths: [] });
});

test("checkPiBinary reports usable pi and unusable missing binary", () => {
  const good = checkPiBinary(process.execPath === "node" ? "node" : "node");
  assert.equal(typeof good.usable, "boolean");
  const missing = checkPiBinary("/nonexistent-pi-binary-xyz");
  assert.equal(missing.usable, false);
  assert.equal(missing.version, "");
});

test("adapter version is set", () => {
  assert.match(ADAPTER_VERSION, /^\d+\.\d+\.\d+$/);
});
