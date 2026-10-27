// Bridge security profile rules for Pi: the shared vectors in
// tests/fixtures/profile_vectors.json (also checked by the Python side),
// plus how Pi's built-in tools map onto the rules.
import assert from "node:assert/strict";
import { test } from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  BUILTINS,
  SHELL_TOOL,
  commandMode,
  evaluateToolCall,
  extractBashCommand,
  fixedProtected,
  fromWire,
  globMatches,
  pathMode,
  profileRevision,
  toolExposure,
  validateRules,
} from "../policy.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const VECTORS = JSON.parse(fs.readFileSync(
  path.join(here, "..", "..", "..", "tests", "fixtures", "profile_vectors.json"), "utf8"));

function makeLayout() {
  const base = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "pi-profile-")));
  for (const dir of VECTORS.layout.dirs) fs.mkdirSync(path.join(base, dir), { recursive: true });
  for (const [link, target] of Object.entries(VECTORS.layout.symlinks)) {
    fs.symlinkSync(target, path.join(base, link));
  }
  return { ws: path.join(base, "ws"), out: path.join(base, "out") };
}

const fill = (value, out) => JSON.parse(JSON.stringify(value).replaceAll("/@out", out));

test("vectors: canonical rules and revisions match the Bridge", () => {
  for (const item of VECTORS.canonical) {
    const rules = validateRules(item.raw);
    assert.deepEqual(rules, item.rules);
    assert.equal(profileRevision(rules), item.revision);
  }
});

test("vectors: invalid rules are refused", () => {
  for (const raw of VECTORS.invalid) {
    assert.throws(() => validateRules(raw), { name: "PolicyError" }, JSON.stringify(raw));
  }
});

test("vectors: globs, fixed protected paths and commands", () => {
  for (const item of VECTORS.globs) {
    assert.equal(globMatches(item.pattern, item.path), item.match, `${item.pattern} ${item.path}`);
  }
  for (const item of VECTORS.fixed) {
    assert.equal(fixedProtected(item.path) !== null, item.protected, item.path);
  }
  for (const item of VECTORS.commands) {
    const rules = VECTORS.profiles[item.profile];
    assert.equal(commandMode(rules, item.command)[0], item.mode,
      `${item.profile}: ${item.command.slice(0, 40)}`);
  }
});

test("vectors: path modes", () => {
  const layout = makeLayout();
  for (const item of VECTORS.paths) {
    const rules = fill(VECTORS.profiles[item.profile], layout.out);
    const raw = item.path.replaceAll("/@out", layout.out);
    assert.equal(pathMode(rules, layout.ws, raw, { write: item.write })[0], item.mode,
      `${item.profile}: ${item.path} write=${item.write}`);
  }
});

test("fromWire accepts the Bridge block and refuses tampering", () => {
  const rules = BUILTINS.reviewed;
  const block = { source: "profile", profile: { id: "reviewed",
    revision: profileRevision(rules), rules } };
  assert.deepEqual(fromWire(block), { id: "reviewed", revision: block.profile.revision, rules });
  const tampered = structuredClone(block);
  tampered.profile.rules.shell = "allow";
  for (const bad of [tampered, { source: "native" }, null,
    { source: "profile", profile: { ...block.profile, id: "Bad ID" } }]) {
    assert.throws(() => fromWire(bad), { name: "PolicyError" });
  }
});

test("tool exposure hides tools the rules can never allow", () => {
  assert.deepEqual(toolExposure(BUILTINS["read-only"]), { writable: false, shellMode: "deny" });
  assert.deepEqual(toolExposure(BUILTINS.reviewed), { writable: true, shellMode: "ask" });
  const listOnly = validateRules({ ...BUILTINS["read-only"],
    commands: [{ prefix: "ls", mode: "allow" }] });
  assert.deepEqual(toolExposure(listOnly), { writable: false, shellMode: "ask" });
});

test("file tools are judged by their path", () => {
  const layout = makeLayout();
  fs.writeFileSync(path.join(layout.ws, "src", "a.py"), "x\n");
  const policy = BUILTINS.reviewed;
  const call = (toolName, input) => evaluateToolCall({ cwd: layout.ws, policy, toolName, input });
  assert.equal(call("read", { path: "src/a.py" }).effect, "allow");
  assert.equal(call("ls", {}).resource, ".");
  assert.equal(call("grep", { pattern: "x" }).effect, "allow");
  const edit = call("edit", { path: "src/a.py" });
  assert.equal(edit.effect, "allow");
  assert.equal(edit.code, "tool_allow");
  const outside = call("read", { path: path.join(layout.out, "x") });
  assert.equal(outside.effect, "ask");
  assert.equal(outside.resource, path.join(layout.out, "x"));
  assert.equal(outside.grantKey, `read\n${path.join(layout.out, "x")}`);
  assert.equal(call("write", { path: path.join(layout.out, "x") }).effect, "deny");
  const env = call("read", { path: ".env" });
  assert.equal(env.effect, "deny");
  assert.equal(env.code, "protected_path");
  assert.equal(call("read", {}).code, "malformed_input");
  assert.equal(call("read", { path: "a\0b" }).code, "malformed_path");
  assert.equal(call("powershell", { command: "ls" }).code, "unknown_tool");
  assert.equal(evaluateToolCall({ cwd: layout.ws, policy: { shell: "allow" }, toolName: "read",
    input: { path: "src/a.py" } }).code, "invalid_policy");
});

test("bash follows command rules and the shell mode", () => {
  const policy = validateRules({ ...BUILTINS.reviewed,
    commands: [{ prefix: "git push", mode: "deny" }, { prefix: "npm test", mode: "allow" }] });
  const call = (command, extra = {}) => evaluateToolCall({ cwd: os.tmpdir(), policy,
    toolName: SHELL_TOOL, input: { command, ...extra } });
  assert.equal(call("npm test").effect, "allow");
  assert.equal(call("cd x && git push").effect, "deny");
  const ask = call("make", { timeout: 5 });
  assert.equal(ask.effect, "ask");
  const extracted = extractBashCommand({ command: "make", timeout: 5 });
  assert.equal(ask.grantKey, `bash\n${extracted.commandHash}\n5000`);
  assert.equal(call("x".repeat(16385)).effect, "deny");
  assert.equal(evaluateToolCall({ cwd: os.tmpdir(), policy, toolName: SHELL_TOOL, input: {} }).code,
    "malformed_input");
});
