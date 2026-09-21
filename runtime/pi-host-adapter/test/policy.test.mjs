// 3C1 policy v3 validation/defaults/tool modes/pattern validation and the
// shared evaluator: workspace/external classification, external default +
// root overrides, composition, symlink escape, protected patterns
// (admin-configured), allow/ask/deny, exact grant scope, shell
// deny/ask/allow with no command rules, and removal of fixed filesystem
// denies (no self-protection/control-dir/project-specific rules).
import assert from "node:assert/strict";
import { test } from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import {
  SHELL_TOOL,
  canonicalizeExternalRoots,
  canonicalJson,
  evaluateToolCall,
  extractBashCommand,
  globToRegExp,
  policyRevision,
  safeDefaultPolicy,
  validatePolicy,
} from "../policy.mjs";

function makeWorkspace() {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "pi-policy-"));
  const root = fs.realpathSync(tmp);
  const app = path.join(root, "app");
  fs.mkdirSync(app, { recursive: true });
  const cwd = fs.realpathSync(app);
  fs.writeFileSync(path.join(cwd, "notes.txt"), "hello\n");
  return { tmp, root, cwd };
}

function writablePolicy(overrides = {}) {
  return validatePolicy({
    version: 3,
    write_tools_enabled: true,
    tools: { read: "allow", grep: "allow", find: "allow", ls: "allow", edit: "ask", write: "ask" },
    protected_patterns: [".git/**", ".env", ".env.*", ".workspace-handoff/**"],
    protected_template_exceptions: [".env.example", ".env.sample", ".env.template"],
    allow_session_always: true,
    external_access: { default_mode: "deny", roots: [] },
    shell_mode: "deny",
    ...overrides,
  });
}

function externalRootDir() {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "pi-external-"));
  return fs.realpathSync(tmp);
}

test("safe defaults are read-only, external-deny, shell-deny, and validate cleanly", () => {
  const policy = safeDefaultPolicy();
  assert.equal(policy.version, 3);
  assert.equal(policy.write_tools_enabled, false);
  assert.deepEqual(policy.tools,
    { read: "allow", grep: "allow", find: "allow", ls: "allow", edit: "ask", write: "ask" });
  assert.deepEqual(policy.external_access, { default_mode: "deny", roots: [] });
  assert.equal(policy.shell_mode, "deny");
  assert.deepEqual(validatePolicy(policy), policy);
  assert.deepEqual(validatePolicy(JSON.parse(JSON.stringify(policy))), policy);
  const revision = policyRevision(policy);
  assert.match(revision, /^[0-9a-f]{64}$/);
  assert.equal(revision, policyRevision(safeDefaultPolicy()));
});

test("canonical JSON matches the Bridge Python canonicalization", () => {
  // Byte-for-byte expectation shared with the Python 3C1 tests: sorted
  // keys, no spaces. Any drift here breaks revision agreement.
  assert.equal(canonicalJson(safeDefaultPolicy()),
    '{"allow_session_always":true,'
    + '"external_access":{"default_mode":"deny","roots":[]},'
    + '"protected_patterns":[".git/**",".env",".env.*",".workspace-handoff/**"],'
    + '"protected_template_exceptions":[".env.example",".env.sample",".env.template"],'
    + '"shell_mode":"deny",'
    + '"tools":{"edit":"ask","find":"allow","grep":"allow","ls":"allow","read":"allow","write":"ask"},'
    + '"version":3,"write_tools_enabled":false}');
});

test("v2 payloads are rejected, not reinterpreted (coordinated upgrade)", () => {
  const base = safeDefaultPolicy();
  const { shell_mode, ...v2shape } = base;
  void shell_mode;
  assert.throws(() => validatePolicy({ ...v2shape, version: 2 }));
  assert.throws(() => validatePolicy({ ...base, version: 2 }));
  assert.throws(() => validatePolicy({ ...base, version: 1 }));
});

test("validation rejects unknown fields, versions, tools, modes, shell, and external shapes", () => {
  const base = safeDefaultPolicy();
  assert.throws(() => validatePolicy({ ...base, extra: 1 }));
  assert.throws(() => validatePolicy({ ...base, version: 1 }));
  assert.throws(() => validatePolicy({ ...base, version: 2 }));
  assert.throws(() => validatePolicy({ ...base, enabled: true }));
  assert.throws(() => validatePolicy({ ...base, shell_mode: "sometimes" }));
  assert.throws(() => validatePolicy({ ...base, shell_mode: undefined }));
  for (const bad of ["allowlist", "deny .*", "/bin/ls", ""]) {
    void bad;
  }
  // No command rule list exists: any command-rule field is unknown.
  assert.throws(() => validatePolicy({ ...base, command_rules: [] }));
  assert.throws(() => validatePolicy({ ...base, shell_allowlist: [] }));
  assert.throws(() => validatePolicy({ ...base, write_tools_enabled: "yes" }));
  assert.throws(() => validatePolicy({
    ...base, tools: { read: "allow", grep: "allow", find: "allow", ls: "allow", edit: "ask" },
  }));
  assert.throws(() => validatePolicy({
    ...base, tools: { ...base.tools, edit: "sometimes", bogus: "allow" },
  }));
  assert.throws(() => validatePolicy({ ...base, allow_session_always: 1 }));
  assert.throws(() => validatePolicy("not json"));
  assert.throws(() => validatePolicy(null));
  // External access bounds.
  assert.throws(() => validatePolicy({ ...base, external_access: null }));
  assert.throws(() => validatePolicy({
    ...base, external_access: { default_mode: "sometimes", roots: [] },
  }));
  assert.throws(() => validatePolicy({
    ...base, external_access: { default_mode: "deny", roots: "nope" },
  }));
  for (const badPath of ["relative/path", "", "a/../b", "/a//b", "/trailing/", "x".repeat(2000)]) {
    assert.throws(() => validatePolicy({
      ...base, external_access: { default_mode: "deny", roots: [{ path: badPath, mode: "allow" }] },
    }), undefined, badPath);
  }
  for (const badMode of ["sometimes", "", null]) {
    assert.throws(() => validatePolicy({
      ...base, external_access: { default_mode: "deny", roots: [{ path: "/tmp", mode: badMode }] },
    }));
  }
  assert.throws(() => validatePolicy({
    ...base,
    external_access: {
      default_mode: "deny",
      roots: [{ path: "/tmp", mode: "allow" }, { path: "/tmp", mode: "deny" }],
    },
  }));
  const tooMany = Array.from({ length: 33 }, (_, i) => ({ path: `/tmp/root-${i}`, mode: "ask" }));
  assert.throws(() => validatePolicy({
    ...base, external_access: { default_mode: "deny", roots: tooMany },
  }));
  // Valid roots pass.
  const ok = validatePolicy({
    ...base,
    external_access: { default_mode: "ask", roots: [{ path: "/", mode: "deny" }, { path: "/tmp", mode: "allow" }] },
  });
  assert.equal(ok.external_access.default_mode, "ask");
  assert.equal(ok.external_access.roots.length, 2);
});

test("pattern validation rejects absolute paths, traversal, and bad syntax", () => {
  const base = safeDefaultPolicy();
  for (const bad of ["/abs/**", "../escape/**", "a/**/..", "x\0y", "", "a[bc", "semi;colon"]) {
    assert.throws(() => validatePolicy({ ...base, protected_patterns: [bad] }), undefined, bad);
  }
  const tooMany = Array.from({ length: 65 }, (_, i) => `file-${i}.txt`);
  assert.throws(() => validatePolicy({ ...base, protected_patterns: tooMany }));
  assert.throws(() => validatePolicy({ ...base, protected_patterns: ["x".repeat(401)] }));
  // Valid workspace-relative globs pass and dedupe.
  const ok = validatePolicy({
    ...base,
    protected_patterns: ["dist/**", "dist/**", "*.log", "docs/*.md", "file[0-9].txt"],
  });
  assert.deepEqual(ok.protected_patterns, ["dist/**", "*.log", "docs/*.md", "file[0-9].txt"]);
});

test("glob matching honors **, *, ?, and trailing /** base", () => {
  assert.ok(globToRegExp(".git/**").test(".git/config"));
  assert.ok(globToRegExp(".git/**").test(".git/objects/a/b"));
  assert.ok(globToRegExp(".env.*").test(".env.local"));
  assert.ok(!globToRegExp(".env.*").test(".env"));
  assert.ok(globToRegExp("*.log").test("debug.log"));
  assert.ok(!globToRegExp("*.log").test("sub/debug.log"));
  assert.ok(globToRegExp("src/**/*.ts").test("src/a/b/c.ts"));
  assert.ok(globToRegExp("file?.txt").test("file1.txt"));
  assert.ok(!globToRegExp("file?.txt").test("file12.txt"));
  assert.ok(globToRegExp("file[0-9].txt").test("file4.txt"));
});

test("evaluator allows configured read tools and asks edit/write", () => {
  const { cwd } = makeWorkspace();
  const policy = writablePolicy();
  const read = evaluateToolCall({ cwd, policy, toolName: "read", input: { path: "notes.txt" } });
  assert.equal(read.effect, "allow");
  assert.equal(read.resource, "notes.txt");
  const edit = evaluateToolCall({ cwd, policy, toolName: "edit", input: { path: "notes.txt" } });
  assert.equal(edit.effect, "ask");
  assert.equal(edit.resource, "notes.txt");
  assert.equal(edit.alwaysPattern, "edit:notes.txt");
  assert.equal(edit.grantKey, "edit\nnotes.txt");
  assert.deepEqual(edit.requested, ["notes.txt"]);
  const write = evaluateToolCall({ cwd, policy, toolName: "write", input: { path: "new/deep/file.txt" } });
  assert.equal(write.effect, "ask");
  assert.equal(write.resource, "new/deep/file.txt");
  // New write target under cwd resolves via nearest existing ancestor.
  assert.equal(write.code, "tool_ask");
});

test("all six tool modes govern remaining safe resources", () => {
  const { cwd } = makeWorkspace();
  const askAll = writablePolicy({
    tools: { read: "ask", grep: "ask", find: "ask", ls: "ask", edit: "ask", write: "ask" },
  });
  const read = evaluateToolCall({ cwd, policy: askAll, toolName: "read", input: { path: "notes.txt" } });
  assert.equal(read.effect, "ask");
  assert.equal(read.alwaysPattern, "read:notes.txt");
  const grep = evaluateToolCall({ cwd, policy: askAll, toolName: "grep", input: { pattern: "hi" } });
  assert.equal(grep.effect, "ask");
  assert.equal(grep.resource, ".");
  const denyAll = writablePolicy({
    tools: { read: "deny", grep: "deny", find: "deny", ls: "deny", edit: "deny", write: "deny" },
  });
  for (const tool of ["read", "grep", "find", "ls", "edit", "write"]) {
    const input = tool === "grep" ? { pattern: "x" } : tool === "find" ? { pattern: "*.txt" } : { path: "notes.txt" };
    const verdict = evaluateToolCall({ cwd, policy: denyAll, toolName: tool, input });
    assert.equal(verdict.effect, "deny", tool);
    assert.equal(verdict.alwaysPattern, "");
    assert.equal(verdict.grantKey, "");
  }
});

test("protected patterns are a hard deny with template exceptions", () => {
  const { cwd } = makeWorkspace();
  fs.writeFileSync(path.join(cwd, ".env"), "SECRET=1\n");
  fs.writeFileSync(path.join(cwd, ".env.example"), "SECRET=\n");
  const policy = writablePolicy({
    tools: { read: "allow", grep: "allow", find: "allow", ls: "allow", edit: "allow", write: "allow" },
  });
  // Even mode=allow cannot approve a protected target.
  const blocked = evaluateToolCall({ cwd, policy, toolName: "write", input: { path: ".env" } });
  assert.equal(blocked.effect, "deny");
  assert.equal(blocked.code, "protected_pattern");
  assert.equal(blocked.alwaysPattern, "");
  const readBlocked = evaluateToolCall({ cwd, policy, toolName: "read", input: { path: ".env" } });
  assert.equal(readBlocked.effect, "deny");
  // Template exceptions waive the match.
  const allowed = evaluateToolCall({ cwd, policy, toolName: "write", input: { path: ".env.example" } });
  assert.equal(allowed.effect, "allow");
});

test("default external deny governs outside, ../escape, and symlink targets", () => {
  const { cwd } = makeWorkspace();
  const policy = writablePolicy();
  const outside = evaluateToolCall({ cwd, policy, toolName: "read", input: { path: "/etc/hostname" } });
  assert.equal(outside.effect, "deny");
  assert.equal(outside.code, "external_deny");
  assert.ok(path.isAbsolute(outside.resource));
  const dotdot = evaluateToolCall({ cwd, policy, toolName: "write", input: { path: "../../evil.txt" } });
  assert.equal(dotdot.effect, "deny");
  // Symlink inside the workspace pointing outside: the canonical target
  // is governed by external policy (deny by default here).
  const outsideFile = path.join(path.dirname(cwd), "outside-secret.txt");
  fs.writeFileSync(outsideFile, "secret\n");
  fs.symlinkSync(outsideFile, path.join(cwd, "link.txt"));
  const link = evaluateToolCall({ cwd, policy, toolName: "read", input: { path: "link.txt" } });
  assert.equal(link.effect, "deny");
  assert.equal(link.resource, fs.realpathSync(outsideFile));
  // New write target through a symlinked ancestor directory.
  const outsideDir = path.join(path.dirname(cwd), "outside-dir");
  fs.mkdirSync(outsideDir, { recursive: true });
  fs.symlinkSync(outsideDir, path.join(cwd, "dirlink"));
  const through = evaluateToolCall({ cwd, policy, toolName: "write", input: { path: "dirlink/new.txt" } });
  assert.equal(through.effect, "deny");
});

test("external default ask/allow composes with per-tool mode (deny > ask > allow)", () => {
  const { cwd } = makeWorkspace();
  const target = path.join(path.dirname(cwd), "ext-target.txt");
  fs.writeFileSync(target, "external\n");
  // read=allow + external ask => ask with exact canonical scope.
  const askDefault = writablePolicy({ external_access: { default_mode: "ask", roots: [] } });
  const asked = evaluateToolCall({ cwd, policy: askDefault, toolName: "read", input: { path: target } });
  assert.equal(asked.effect, "ask");
  assert.equal(asked.resource, fs.realpathSync(target));
  assert.equal(asked.alwaysPattern, `read:${fs.realpathSync(target)}`);
  assert.equal(asked.grantKey, `read\n${fs.realpathSync(target)}`);
  assert.deepEqual(asked.requested, [fs.realpathSync(target)]);
  // edit=ask + external allow => ask.
  const allowDefault = writablePolicy({ external_access: { default_mode: "allow", roots: [] } });
  const editAsk = evaluateToolCall({ cwd, policy: allowDefault, toolName: "edit", input: { path: target } });
  assert.equal(editAsk.effect, "ask");
  // write=allow + external allow => allow.
  const writeAllow = writablePolicy({
    tools: { read: "allow", grep: "allow", find: "allow", ls: "allow", edit: "ask", write: "allow" },
    external_access: { default_mode: "allow", roots: [] },
  });
  const written = evaluateToolCall({ cwd, policy: writeAllow, toolName: "write", input: { path: target } });
  assert.equal(written.effect, "allow");
  // any deny => deny: tool deny beats external allow.
  const toolDeny = writablePolicy({
    tools: { read: "deny", grep: "allow", find: "allow", ls: "allow", edit: "ask", write: "ask" },
    external_access: { default_mode: "allow", roots: [] },
  });
  const denied = evaluateToolCall({ cwd, policy: toolDeny, toolName: "read", input: { path: target } });
  assert.equal(denied.effect, "deny");
  assert.equal(denied.code, "tool_deny");
});

test("root overrides apply with most-specific match winning", () => {
  const { cwd } = makeWorkspace();
  const parent = externalRootDir();
  const child = path.join(parent, "child");
  fs.mkdirSync(child, { recursive: true });
  fs.writeFileSync(path.join(parent, "top.txt"), "top\n");
  fs.writeFileSync(path.join(child, "nested.txt"), "nested\n");
  const policy = writablePolicy({
    external_access: {
      default_mode: "deny",
      roots: [
        { path: parent, mode: "allow" },
        { path: child, mode: "ask" },
      ],
    },
  });
  const top = evaluateToolCall({ cwd, policy, toolName: "read", input: { path: path.join(parent, "top.txt") } });
  assert.equal(top.effect, "allow");
  const nested = evaluateToolCall({ cwd, policy, toolName: "read", input: { path: path.join(child, "nested.txt") } });
  assert.equal(nested.effect, "ask");
  assert.equal(nested.alwaysPattern, `read:${fs.realpathSync(path.join(child, "nested.txt"))}`);
  const elsewhere = path.join(path.dirname(parent), "pi-sibling-nope.txt");
  fs.writeFileSync(elsewhere, "x\n");
  try {
    const other = evaluateToolCall({ cwd, policy, toolName: "read", input: { path: elsewhere } });
    assert.equal(other.effect, "deny");
  } finally {
    fs.unlinkSync(elsewhere);
  }
});

test("explicit root '/' governs the whole host when configured", () => {
  const { cwd } = makeWorkspace();
  const policy = writablePolicy({
    tools: { read: "allow", grep: "allow", find: "allow", ls: "allow", edit: "ask", write: "ask" },
    external_access: { default_mode: "deny", roots: [{ path: "/", mode: "allow" }] },
  });
  const target = path.join(path.dirname(cwd), "outside-secret.txt");
  fs.writeFileSync(target, "secret\n");
  const verdict = evaluateToolCall({ cwd, policy, toolName: "read", input: { path: target } });
  assert.equal(verdict.effect, "allow");
  assert.equal(verdict.code, "external_allow");
});

test("canonical root validation rejects missing dirs and canonical duplicates", () => {
  const parent = externalRootDir();
  assert.throws(() => canonicalizeExternalRoots([{ path: "/no-such-pi-root-xyz", mode: "allow" }]));
  assert.throws(() => canonicalizeExternalRoots([
    { path: parent, mode: "allow" },
    { path: parent, mode: "deny" },
  ]));
  // Lexical aliases of the same directory are canonical duplicates too.
  assert.throws(() => canonicalizeExternalRoots([
    { path: parent, mode: "allow" },
    { path: `${parent}/./`, mode: "allow" },
  ]));
  const ok = canonicalizeExternalRoots([{ path: parent, mode: "ask" }]);
  assert.equal(ok.length, 1);
  assert.equal(ok[0].mode, "ask");
});

test("symlink escape from under an allowed root uses the resolved location", () => {
  const { cwd } = makeWorkspace();
  const allowed = externalRootDir();
  const secret = path.join(path.dirname(allowed), "pi-escape-secret.txt");
  fs.writeFileSync(secret, "secret\n");
  fs.symlinkSync(secret, path.join(allowed, "escape-link.txt"));
  const policy = writablePolicy({
    external_access: { default_mode: "deny", roots: [{ path: allowed, mode: "allow" }] },
  });
  try {
    const verdict = evaluateToolCall({
      cwd, policy, toolName: "read", input: { path: path.join(allowed, "escape-link.txt") },
    });
    assert.equal(verdict.effect, "deny");
    assert.equal(verdict.resource, fs.realpathSync(secret));
  } finally {
    fs.unlinkSync(secret);
  }
});

test("relative ../external paths are governed by external policy, not malformed", () => {
  const { cwd } = makeWorkspace();
  const sibling = path.join(path.dirname(cwd), "sibling.txt");
  fs.writeFileSync(sibling, "sibling\n");
  const askDefault = writablePolicy({ external_access: { default_mode: "ask", roots: [] } });
  const verdict = evaluateToolCall({ cwd, policy: askDefault, toolName: "read", input: { path: "../sibling.txt" } });
  assert.equal(verdict.effect, "ask");
  assert.equal(verdict.resource, fs.realpathSync(sibling));
});

test("protected patterns and exceptions apply to external paths", () => {
  const { cwd } = makeWorkspace();
  const allowed = externalRootDir();
  fs.writeFileSync(path.join(allowed, ".env"), "SECRET=1\n");
  fs.writeFileSync(path.join(allowed, ".env.example"), "SECRET=\n");
  fs.mkdirSync(path.join(allowed, "sub"), { recursive: true });
  fs.writeFileSync(path.join(allowed, "sub", "private.key"), "SECRET=1\n");
  const policy = writablePolicy({
    tools: { read: "allow", grep: "allow", find: "allow", ls: "allow", edit: "allow", write: "allow" },
    protected_patterns: [".git/**", ".env", ".env.*", ".workspace-handoff/**", "**/*.key"],
    protected_template_exceptions: [".env.example", ".env.sample", ".env.template"],
    external_access: { default_mode: "allow", roots: [{ path: allowed, mode: "allow" }] },
  });
  const blocked = evaluateToolCall({ cwd, policy, toolName: "read", input: { path: path.join(allowed, ".env") } });
  assert.equal(blocked.effect, "deny");
  assert.equal(blocked.code, "protected_pattern");
  const nested = evaluateToolCall({
    cwd, policy, toolName: "read", input: { path: path.join(allowed, "sub", "private.key") },
  });
  assert.equal(nested.effect, "deny");
  const exception = evaluateToolCall({
    cwd, policy, toolName: "read", input: { path: path.join(allowed, ".env.example") },
  });
  assert.equal(exception.effect, "allow");
});

test("v3 has no fixed control-directory deny: ordinary policy applies", () => {
  const { cwd } = makeWorkspace();
  const control = externalRootDir();
  fs.writeFileSync(path.join(control, "token.json"), "secret\n");
  const policy = writablePolicy({
    tools: { read: "allow", grep: "allow", find: "allow", ls: "allow", edit: "allow", write: "allow" },
    external_access: { default_mode: "allow", roots: [{ path: control, mode: "allow" }] },
  });
  // Former controlDirs/selfProtectedDirs params are ignored in v3.
  for (const tool of ["read", "grep", "find", "ls", "edit", "write"]) {
    const input = tool === "grep" ? { pattern: "x", path: path.join(control, "token.json") }
      : tool === "find" ? { pattern: "*.json", path: control }
      : { path: path.join(control, "token.json") };
    const verdict = evaluateToolCall({
      cwd, policy, toolName: tool, input, controlDirs: [control],
      selfProtectedDirs: [control],
    });
    assert.equal(verdict.effect, "allow", tool);
  }
});

test("v3 has no fixed self-protection deny: project edits are ordinary policy", () => {
  const { cwd } = makeWorkspace();
  const policy = writablePolicy({
    tools: { read: "allow", grep: "allow", find: "allow", ls: "allow", edit: "allow", write: "allow" },
  });
  // Even the adapter package dir is governed by ordinary configurable
  // policy in v3 (enforcement is identified by fingerprint, not by deny).
  const verdict = evaluateToolCall({
    cwd, policy, toolName: "write", input: { path: "notes.txt" },
    selfProtectedDirs: [cwd],
  });
  assert.equal(verdict.effect, "allow");
  assert.notEqual(verdict.code, "self_protected");
  assert.notEqual(verdict.code, "control_protected");
});

test("shell deny/ask/allow with exact-command scope and no command rules", () => {
  const { cwd } = makeWorkspace();
  assert.equal(SHELL_TOOL, "bash");
  const deny = writablePolicy({ shell_mode: "deny" });
  const denied = evaluateToolCall({ cwd, policy: deny, toolName: "bash", input: { command: "ls -la" } });
  assert.equal(denied.effect, "deny");
  assert.equal(denied.code, "shell_deny");
  const ask = writablePolicy({ shell_mode: "ask" });
  const asked = evaluateToolCall({ cwd, policy: ask, toolName: "bash", input: { command: "ls -la", timeoutMs: 5000 } });
  assert.equal(asked.effect, "ask");
  assert.equal(asked.code, "shell_ask");
  assert.ok(asked.resource.includes("ls -la"));
  assert.ok(asked.grantKey.startsWith("bash\n"));
  assert.ok(asked.alwaysPattern.startsWith("bash:"));
  assert.equal(asked.timeoutMs, 5000);
  assert.ok(asked.commandHash?.match(/^[0-9a-f]{64}$/));
  // Same command + same timeout re-evaluates identically (session-local
  // exact-command grant scope); a different command does not match.
  const again = evaluateToolCall({ cwd, policy: ask, toolName: "bash", input: { command: "ls -la", timeoutMs: 5000 } });
  assert.equal(again.grantKey, asked.grantKey);
  const different = evaluateToolCall({ cwd, policy: ask, toolName: "bash", input: { command: "ls -lb", timeoutMs: 5000 } });
  assert.notEqual(different.grantKey, asked.grantKey);
  const allow = writablePolicy({ shell_mode: "allow" });
  const allowed = evaluateToolCall({ cwd, policy: allow, toolName: "bash", input: { command: "ls -la" } });
  assert.equal(allowed.effect, "allow");
  assert.equal(allowed.code, "shell_allow");
  // No powershell, no command regex/prefix system: unknown shells fail.
  const ps = evaluateToolCall({ cwd, policy: allow, toolName: "powershell", input: { command: "ls" } });
  assert.equal(ps.effect, "deny");
  assert.equal(ps.code, "unknown_tool");
  // Malformed bash input fails closed.
  for (const bad of [null, {}, { command: "" }, { command: 42 }]) {
    const verdict = evaluateToolCall({ cwd, policy: ask, toolName: "bash", input: bad });
    assert.equal(verdict.effect, "deny");
  }
  // extractBashCommand bounds the command and verifies timeout.
  const big = extractBashCommand({ command: "x".repeat(20000), timeoutMs: 9999999 });
  assert.equal(big.ok, true);
  assert.equal(big.command.length, 16384);
  assert.equal(big.truncated, true);
  assert.equal(big.timeoutMs, 300000);
});

test("malformed and unknown tool input fails closed", () => {
  const { cwd } = makeWorkspace();
  const policy = writablePolicy();
  for (const bad of [null, undefined, "notes.txt", [], {}, { path: "" }, { path: 42 },
      { path: "a\0b" }, { path: "x".repeat(5000) }]) {
    const verdict = evaluateToolCall({ cwd, policy, toolName: "read", input: bad });
    assert.equal(verdict.effect, "deny", JSON.stringify(bad)?.slice(0, 40));
    assert.ok(["malformed_input", "malformed_path"].includes(verdict.code));
  }
  const unknown = evaluateToolCall({ cwd, policy, toolName: "powershell", input: { command: "ls" } });
  assert.equal(unknown.effect, "deny");
  assert.equal(unknown.code, "unknown_tool");
});

test("outputs are bounded and carry exact scope fields", () => {
  const { cwd } = makeWorkspace();
  const policy = writablePolicy();
  const verdict = evaluateToolCall({ cwd, policy, toolName: "edit", input: { path: "notes.txt" } });
  assert.ok(verdict.reason.length <= 200);
  assert.ok(verdict.resource.length <= 400);
  assert.ok(!JSON.stringify(verdict).includes("hello"));
});
