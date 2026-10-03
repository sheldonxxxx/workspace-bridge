// Focused login-shell PATH resolution and Pi startup ordering.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  MAX_LOGIN_PATH_CHARS,
  MAX_LOGIN_SHELL_OUTPUT,
  applyLoginPathResult,
  getLoginShell,
  loginPathSummary,
  probeArgv,
  probeCommandForShell,
  resolveLoginPathSync,
  shellBasename,
  validateSearchPath,
} from "../login-path.mjs";
import { buildMain } from "../main.mjs";

function fakeSpawn(stdout, { status = 0, error = null } = {}) {
  return (shell, args, options) => {
    // Interactive-login invocation: `-l -i -c <probe>` for full shells,
    // conservative `-i -c <probe>` for the minimal sh family.
    assert.ok(args.includes("-i"));
    assert.ok(args.includes("-c"));
    const probe = args[args.length - 1];
    assert.ok(probe.includes("printf"));
    assert.ok(probe.includes("__WB_LOGIN_PATH_BEGIN__"));
    assert.ok(probe.includes(">&3"));
    assert.ok(typeof options.timeout === "number");
    // Explicit hard bound at the capture boundary: dedicated fd-3 pipe plus
    // capped maxBuffer; ordinary stdout/stderr stay discarded/isolated.
    assert.ok(options.maxBuffer <= MAX_LOGIN_SHELL_OUTPUT);
    assert.deepEqual(options.stdio, ["ignore", "ignore", "ignore", "pipe"]);
    const base = String(shell).split("/").pop().toLowerCase();
    if (["sh", "dash", "ash"].includes(base)) {
      assert.deepEqual(args.slice(0, 2), ["-i", "-c"]);
    } else {
      assert.deepEqual(args.slice(0, 3), ["-l", "-i", "-c"]);
    }
    return { stdout, status, error };
  };
}

test("success validates and summary exposes only safe fields", () => {
  const result = resolveLoginPathSync({
    shellOverride: "/bin/zsh",
    spawnSyncFn: fakeSpawn("__WB_LOGIN_PATH_BEGIN__/usr/bin:/bin:/opt/tools/bin__WB_LOGIN_PATH_END__"),
  });
  assert.equal(result.resolved, true);
  assert.equal(result.path, "/usr/bin:/bin:/opt/tools/bin");
  assert.equal(result.shell, "/bin/zsh");
  assert.equal(result.shellBasename, "zsh");
  assert.equal(result.entryCount, 3);
  assert.equal(result.code, "ok");
  const summary = loginPathSummary(result);
  assert.deepEqual(summary, { resolved: true, shellBasename: "zsh", entryCount: 3, code: "ok" });
  assert.ok(!JSON.stringify(summary).includes("/usr/bin"));
  assert.deepEqual(validateSearchPath(result.path), {
    ok: true, entries: ["/usr/bin", "/bin", "/opt/tools/bin"], code: "ok",
  });
});

test("interactive-login invocation captures interactive-only PATH (regression)", () => {
  let seen = null;
  const result = resolveLoginPathSync({
    shellOverride: "/bin/zsh",
    spawnSyncFn: (shell, args) => {
      seen = [...args];
      // Fake shell mimics `.zshrc` (interactive-only): the extra entry
      // appears only when `-i` is present.
      const path = args.includes("-i")
        ? "/usr/bin:/bin:/interactive/tools"
        : "/usr/bin:/bin";
      return {
        stdout: `__WB_LOGIN_PATH_BEGIN__${path}__WB_LOGIN_PATH_END__`,
        status: 0,
      };
    },
  });
  assert.deepEqual(seen.slice(0, 3), ["-l", "-i", "-c"]);
  assert.equal(result.resolved, true);
  assert.equal(result.path, "/usr/bin:/bin:/interactive/tools");
  assert.equal(result.entryCount, 3);
});

test("unsupported shells fail safely and fish uses the documented join form", () => {
  const unsupported = resolveLoginPathSync({
    shellOverride: "/bin/elvish",
    spawnSyncFn: () => { throw new Error("must not spawn unsupported shells"); },
  });
  assert.equal(unsupported.resolved, false);
  assert.equal(unsupported.code, "unsupported");
  assert.ok(probeCommandForShell("/usr/local/bin/fish").includes("string join"));
  assert.ok(probeCommandForShell("/bin/zsh").includes("$PATH"));
  assert.deepEqual(probeArgv("/usr/local/bin/fish").slice(1, 4), ["-l", "-i", "-c"]);
  assert.equal(probeArgv("/bin/elvish"), null);
  assert.deepEqual(probeArgv("/bin/sh").slice(1, 3), ["-i", "-c"]);
});

test("noisy shell output is isolated to the marked value", () => {
  const result = resolveLoginPathSync({
    shellOverride: "/bin/zsh",
    spawnSyncFn: fakeSpawn("Welcome\nmotd\n__WB_LOGIN_PATH_BEGIN__/usr/bin:/bin__WB_LOGIN_PATH_END__\nbye\n"),
  });
  assert.equal(result.resolved, true);
  assert.equal(result.path, "/usr/bin:/bin");
  assert.equal(result.entryCount, 2);
});

test("oversize capture falls back without exposing content", () => {
  const oversize = "x".repeat(MAX_LOGIN_SHELL_OUTPUT + 1024);
  const result = resolveLoginPathSync({
    shellOverride: "/bin/zsh",
    spawnSyncFn: fakeSpawn(oversize),
  });
  assert.equal(result.resolved, false);
  assert.equal(result.code, "output_too_large");
  assert.equal(result.path, null);
  const summary = loginPathSummary(result);
  assert.equal(summary.code, "output_too_large");
  const dumped = JSON.stringify(summary);
  assert.ok(!dumped.includes(oversize.slice(0, 32)));
  assert.ok(!dumped.includes("x".repeat(64)));
  const target = { PATH: "/inherited" };
  applyLoginPathResult(result, target);
  assert.equal(target.PATH, "/inherited");
});

test("maxBuffer breach maps to oversize fallback", () => {
  const result = resolveLoginPathSync({
    shellOverride: "/bin/zsh",
    spawnSyncFn: () => ({ error: Object.assign(new Error("too much"), { code: "ENOBUFS" }) }),
  });
  assert.equal(result.resolved, false);
  assert.equal(result.code, "output_too_large");
});

test("timeout and spawn errors fall back without applying", () => {
  const timedOut = resolveLoginPathSync({
    shellOverride: "/bin/zsh",
    spawnSyncFn: () => ({ error: Object.assign(new Error("timed out"), { code: "ETIMEDOUT" }) }),
  });
  assert.equal(timedOut.resolved, false);
  assert.equal(timedOut.code, "timeout");
  const failed = resolveLoginPathSync({
    shellOverride: "/bin/zsh",
    spawnSyncFn: () => ({ error: new Error("spawn ENOENT") }),
  });
  assert.equal(failed.resolved, false);
  assert.equal(failed.code, "spawn_error");
  const target = { PATH: "/inherited", OTHER: "kept" };
  applyLoginPathResult(timedOut, target);
  assert.equal(target.PATH, "/inherited");
  applyLoginPathResult(failed, target);
  assert.equal(target.PATH, "/inherited");
  assert.equal(target.OTHER, "kept");
});

test("invalid paths are rejected with safe codes", () => {
  const cases = [
    ["", "empty"],
    ["/usr/bin:relative/bin", "relative"],
    ["/usr/bin::/bin", "empty_entry"],
    ["/usr/bin:.:/bin", "relative"],
    ["/usr/bin:/a/../b", "dot_entry"],
    ["/usr/bin:/bin\n/evil", "control"],
    [`/usr/bin:\x00/bin`, "control"],
    ["x".repeat(MAX_LOGIN_PATH_CHARS + 1), "too_long"],
  ];
  for (const [candidate, code] of cases) {
    const checked = validateSearchPath(candidate);
    assert.equal(checked.ok, false);
    assert.equal(checked.code, code);
    const result = resolveLoginPathSync({
      shellOverride: "/bin/sh",
      spawnSyncFn: fakeSpawn(`__WB_LOGIN_PATH_BEGIN__${candidate}__WB_LOGIN_PATH_END__`),
    });
    assert.equal(result.resolved, false);
    assert.equal(result.path, null);
    assert.ok(!JSON.stringify(loginPathSummary(result)).includes(candidate.slice(0, 20)) || !candidate);
  }
});

test("only PATH is imported and shell basename is safe", () => {
  const target = { PATH: "/inherited", WB_RUNTIME_TOKEN: "tok", HOME: "/home/u" };
  const result = resolveLoginPathSync({
    shellOverride: "/bin/zsh",
    spawnSyncFn: fakeSpawn("__WB_LOGIN_PATH_BEGIN__/usr/bin:/bin__WB_LOGIN_PATH_END__"),
  });
  applyLoginPathResult(result, target);
  assert.equal(target.PATH, "/usr/bin:/bin");
  assert.equal(target.WB_RUNTIME_TOKEN, "tok");
  assert.equal(target.HOME, "/home/u");
  assert.equal(shellBasename("/bin/zsh"), "zsh");
  assert.equal(shellBasename("/weird;`rm`"), "weirdrm");
  assert.equal(getLoginShell({ userInfoShell: "/bin/zsh", shellEnv: "/bin/bash" }), "/bin/zsh");
  assert.equal(getLoginShell({ userInfoShell: "", shellEnv: "/bin/bash" }), "/bin/bash");
  assert.equal(getLoginShell({ userInfoShell: "", shellEnv: "" }), "/bin/sh");
});

test("Pi startup resolves login PATH before Pi binary probing and applies only PATH", () => {
  const calls = [];
  const target = { PATH: "/inherited" };
  const fakeResolved = {
    resolved: true, path: "/usr/bin:/bin:/login/tools",
    shell: "/bin/zsh", shellBasename: "zsh", entryCount: 3, code: "ok",
  };
  const built = buildMain({ WB_PI_PROJECTS_DIR: "", WB_LOG_LEVEL: "INFO" }, {
    resolveLoginPathFn: () => {
      calls.push("resolve");
      return fakeResolved;
    },
    loginPathTargetEnv: target,
    checkPiBinaryFn: (binary) => {
      calls.push(`probe:${target.PATH}`);
      assert.equal(binary, "pi");
      return { usable: false, version: "" };
    },
  });
  assert.deepEqual(calls, ["resolve", "probe:/usr/bin:/bin:/login/tools"]);
  assert.equal(target.PATH, "/usr/bin:/bin:/login/tools");
  assert.equal(built.loginPath, fakeResolved);
  assert.equal(built.piCheck.usable, false);
});

test("Pi startup keeps inherited PATH when resolution fails", () => {
  const target = { PATH: "/inherited" };
  const built = buildMain({ WB_PI_PROJECTS_DIR: "", WB_LOG_LEVEL: "INFO" }, {
    resolveLoginPathFn: () => ({
      resolved: false, path: null, shell: "/bin/zsh",
      shellBasename: "zsh", entryCount: 0, code: "timeout",
    }),
    loginPathTargetEnv: target,
    checkPiBinaryFn: () => ({ usable: false, version: "" }),
  });
  assert.equal(target.PATH, "/inherited");
  assert.equal(built.loginPath.resolved, false);
  assert.equal(built.loginPath.code, "timeout");
});
