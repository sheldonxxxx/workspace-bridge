// 3B1 policy validation/defaults/tool modes/pattern validation and the
// shared evaluator: confinement, symlink escape, self-protection,
// protected patterns/exceptions, allow/ask/deny, exact grant scope.
import assert from "node:assert/strict";
import { test } from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import {
  canonicalJson,
  evaluateToolCall,
  globToRegExp,
  policyRevision,
  safeDefaultPolicy,
  selfProtectionDir,
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
    version: 1,
    enabled: true,
    tools: { read: "allow", grep: "allow", find: "allow", ls: "allow", edit: "ask", write: "ask" },
    protected_patterns: [".git/**", ".env", ".env.*", ".workspace-handoff/**"],
    protected_template_exceptions: [".env.example", ".env.sample", ".env.template"],
    allow_session_always: true,
    ...overrides,
  });
}

test("safe defaults are read-only and validate cleanly", () => {
  const policy = safeDefaultPolicy();
  assert.equal(policy.enabled, false);
  assert.deepEqual(policy.tools,
    { read: "allow", grep: "allow", find: "allow", ls: "allow", edit: "ask", write: "ask" });
  assert.deepEqual(validatePolicy(policy), policy);
  assert.deepEqual(validatePolicy(JSON.parse(JSON.stringify(policy))), policy);
  const revision = policyRevision(policy);
  assert.match(revision, /^[0-9a-f]{64}$/);
  assert.equal(revision, policyRevision(safeDefaultPolicy()));
});

test("canonical JSON matches the Bridge Python canonicalization", () => {
  // Byte-for-byte expectation shared with the Python 3B1 tests: sorted
  // keys, no spaces. Any drift here breaks revision agreement.
  assert.equal(canonicalJson(safeDefaultPolicy()),
    '{"allow_session_always":true,"enabled":false,'
    + '"protected_patterns":[".git/**",".env",".env.*",".workspace-handoff/**"],'
    + '"protected_template_exceptions":[".env.example",".env.sample",".env.template"],'
    + '"tools":{"edit":"ask","find":"allow","grep":"allow","ls":"allow","read":"allow","write":"ask"},'
    + '"version":1}');
});

test("validation rejects unknown fields, versions, tools, and modes", () => {
  const base = safeDefaultPolicy();
  assert.throws(() => validatePolicy({ ...base, extra: 1 }));
  assert.throws(() => validatePolicy({ ...base, version: 2 }));
  assert.throws(() => validatePolicy({ ...base, enabled: "yes" }));
  assert.throws(() => validatePolicy({
    ...base, tools: { read: "allow", grep: "allow", find: "allow", ls: "allow", edit: "ask" },
  }));
  assert.throws(() => validatePolicy({
    ...base, tools: { ...base.tools, edit: "sometimes", bogus: "allow" },
  }));
  assert.throws(() => validatePolicy({ ...base, allow_session_always: 1 }));
  assert.throws(() => validatePolicy("not json"));
  assert.throws(() => validatePolicy(null));
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

test("outside-workspace and symlink escapes are denied", () => {
  const { cwd } = makeWorkspace();
  const policy = writablePolicy();
  const outside = evaluateToolCall({ cwd, policy, toolName: "read", input: { path: "/etc/hostname" } });
  assert.equal(outside.effect, "deny");
  assert.equal(outside.code, "outside_workspace");
  const dotdot = evaluateToolCall({ cwd, policy, toolName: "write", input: { path: "../../evil.txt" } });
  assert.equal(dotdot.effect, "deny");
  assert.equal(dotdot.code, "outside_workspace");
  // Symlink inside the workspace pointing outside: existing-target escape.
  const outsideFile = path.join(path.dirname(cwd), "outside-secret.txt");
  fs.writeFileSync(outsideFile, "secret\n");
  fs.symlinkSync(outsideFile, path.join(cwd, "link.txt"));
  const link = evaluateToolCall({ cwd, policy, toolName: "read", input: { path: "link.txt" } });
  assert.equal(link.effect, "deny");
  assert.equal(link.code, "outside_workspace");
  // New write target through a symlinked ancestor directory.
  const outsideDir = path.join(path.dirname(cwd), "outside-dir");
  fs.mkdirSync(outsideDir, { recursive: true });
  fs.symlinkSync(outsideDir, path.join(cwd, "dirlink"));
  const through = evaluateToolCall({ cwd, policy, toolName: "write", input: { path: "dirlink/new.txt" } });
  assert.equal(through.effect, "deny");
  assert.equal(through.code, "outside_workspace");
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
  const unknown = evaluateToolCall({ cwd, policy, toolName: "bash", input: { command: "ls" } });
  assert.equal(unknown.effect, "deny");
  assert.equal(unknown.code, "unknown_tool");
});

test("permission implementation self-protection denies edit/write", () => {
  const { cwd } = makeWorkspace();
  const policy = writablePolicy({
    tools: { read: "allow", grep: "allow", find: "allow", ls: "allow", edit: "allow", write: "allow" },
  });
  const selfDir = selfProtectionDir();
  assert.ok(selfDir.length > 0);
  // A target inside the package-owned implementation dir is denied even
  // with mode=allow. Simulate by resolving a path under it: use an
  // absolute path operand pointing into the implementation dir.
  const verdict = evaluateToolCall({
    cwd,
    policy,
    toolName: "write",
    input: { path: path.join(selfDir, "policy.mjs") },
    selfProtectedDirs: [selfDir],
  });
  // Absolute outside-workspace input denies first (also fail-closed); the
  // dedicated self-protection code is covered by pointing cwd there.
  assert.equal(verdict.effect, "deny");
  const inside = evaluateToolCall({
    cwd: selfDir,
    policy,
    toolName: "edit",
    input: { path: "policy.mjs" },
    selfProtectedDirs: [selfDir],
  });
  assert.equal(inside.effect, "deny");
  assert.equal(inside.code, "self_protected");
  // The whole package dir is covered, not just one module: the adapter
  // and the trusted extension itself are denied too, for both edit and
  // write, even with mode=allow.
  for (const target of ["adapter.mjs", "trusted-permission-extension.mjs", "rpc.mjs"]) {
    for (const tool of ["edit", "write"]) {
      const verdict = evaluateToolCall({
        cwd: selfDir,
        policy,
        toolName: tool,
        input: { path: target },
        selfProtectedDirs: [selfDir],
      });
      assert.equal(verdict.effect, "deny", `${tool} ${target}`);
      assert.equal(verdict.code, "self_protected", `${tool} ${target}`);
      assert.equal(verdict.alwaysPattern, "");
    }
  }
});

test("outputs are bounded and carry exact scope fields", () => {
  const { cwd } = makeWorkspace();
  const policy = writablePolicy();
  const verdict = evaluateToolCall({ cwd, policy, toolName: "edit", input: { path: "notes.txt" } });
  assert.ok(verdict.reason.length <= 200);
  assert.ok(verdict.resource.length <= 400);
  assert.ok(!JSON.stringify(verdict).includes("hello"));
});
