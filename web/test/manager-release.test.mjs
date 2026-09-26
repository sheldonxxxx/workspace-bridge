import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  BUILD_ID_RE,
  PRODUCTION_TOP_LEVEL_FILES,
  canonicalPyprojectPath,
  collectManagerInputFiles,
  computeManagerBuildId,
  computeManagerRelease,
  parseProductVersionFromPyprojectText,
  readProductVersion,
  validateManagerRelease,
} from "../manager-release.mjs";
import {
  describeManagerIdentity,
  tryValidateManagerRelease,
} from "../src/lib/manager-identity.ts";

const WEB_ROOT = path.resolve(import.meta.dirname, "..");
const REPO_ROOT = path.resolve(WEB_ROOT, "..");

function validIdentity(overrides = {}) {
  return {
    contract: 1,
    product: "workspace-bridge",
    product_version: "0.1.0",
    component: "manager",
    component_version: "0.1.0",
    build_id: `sha256:${"a".repeat(64)}`,
    ...overrides,
  };
}

test("product-version-only change changes Manager build ID", () => {
  const entries = [
    { rel: "src/a.ts", data: Buffer.from("console.log(1)\n") },
    { rel: "package.json", data: Buffer.from("{}\n") },
  ];
  const first = computeManagerBuildId("0.1.0", entries);
  const second = computeManagerBuildId("0.1.1", entries);
  assert.match(first, BUILD_ID_RE);
  assert.match(second, BUILD_ID_RE);
  assert.notEqual(first, second);
});

test("real Manager build ID changes on product-version-only bump", () => {
  const live = computeManagerRelease(WEB_ROOT);
  assert.equal(live.product_version, readProductVersion(WEB_ROOT));
  const entries = liveEntries();
  const bumped = computeManagerBuildId("9.9.9", entries);
  assert.notEqual(live.build_id, bumped);
});

function liveEntries() {
  // Read live production entries without re-resolving the product version.
  const files = collectManagerInputFiles(WEB_ROOT);
  const entries = files.map((file) => ({
    rel: path.relative(WEB_ROOT, file).split(path.sep).join("/"),
    data: fs.readFileSync(file),
  }));
  entries.sort((a, b) => (a.rel < b.rel ? -1 : a.rel > b.rel ? 1 : 0));
  return entries;
}

test("test-only Playwright config does not affect Manager build ID", () => {
  assert.ok(!PRODUCTION_TOP_LEVEL_FILES.includes("playwright.config.ts"));
  assert.ok(!PRODUCTION_TOP_LEVEL_FILES.includes("components.json"));
  const files = collectManagerInputFiles(WEB_ROOT);
  const rels = files.map((f) =>
    path.relative(WEB_ROOT, f).split(path.sep).join("/"),
  );
  assert.ok(!rels.includes("playwright.config.ts"));
  assert.ok(!rels.includes("components.json"));
  // A temp web root with an extra playwright file hashes identically.
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "wb-manager-inputs-"));
  try {
    fs.mkdirSync(path.join(tmp, "src"), { recursive: true });
    fs.mkdirSync(path.join(tmp, "public"), { recursive: true });
    fs.writeFileSync(path.join(tmp, "src", "a.ts"), "export const x = 1;\n");
    fs.writeFileSync(
      path.join(tmp, "package.json"),
      JSON.stringify({ name: "web" }),
    );
    const before = collectManagerInputFiles(tmp).map((f) =>
      path.relative(tmp, f).split(path.sep).join("/"),
    );
    fs.writeFileSync(
      path.join(tmp, "playwright.config.ts"),
      "export default {};\n",
    );
    fs.writeFileSync(path.join(tmp, "components.json"), "{}\n");
    const after = collectManagerInputFiles(tmp).map((f) =>
      path.relative(tmp, f).split(path.sep).join("/"),
    );
    assert.deepEqual(after, before);
    const entries = before.map((rel) => ({
      rel,
      data: fs.readFileSync(path.join(tmp, rel)),
    }));
    assert.equal(
      computeManagerBuildId("0.1.0", entries),
      computeManagerBuildId("0.1.0", entries),
    );
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});

test("local input inventory matches Docker web-builder production inputs", () => {
  const dockerfile = fs.readFileSync(
    path.join(REPO_ROOT, "Dockerfile"),
    "utf8",
  );
  const lines = dockerfile.split("\n");
  const builderStart = lines.findIndex((l) => l.includes("AS web-builder"));
  assert.ok(builderStart >= 0);
  let builderEnd = lines.length;
  for (let i = builderStart + 1; i < lines.length; i++) {
    if (/^FROM\s/.test(lines[i])) {
      builderEnd = i;
      break;
    }
  }
  const stage = lines.slice(builderStart, builderEnd).join("\n");
  // Test-only and shadcn tooling metadata must not be production inputs.
  assert.ok(!stage.includes("web/playwright.config.ts"));
  assert.ok(!stage.includes("web/components.json"));
  // Every production top-level file must be copied from the web directory.
  for (const name of PRODUCTION_TOP_LEVEL_FILES) {
    assert.ok(
      stage.includes(`web/${name}`),
      `Docker web-builder must copy web/${name}`,
    );
  }
  assert.ok(stage.includes("web/public/"));
  assert.ok(stage.includes("web/src/"));
  // Canonical release metadata must reach the exact path Vite reads.
  const canonical = canonicalPyprojectPath(WEB_ROOT);
  assert.equal(canonical, path.resolve(REPO_ROOT, "pyproject.toml"));
  assert.ok(
    stage.includes("pyproject.toml") &&
      (stage.includes("/source/pyproject.toml") ||
        stage.includes("../pyproject.toml")),
    "Docker web-builder must copy root pyproject.toml to /source/pyproject.toml",
  );
  // Every collected local input must be covered by the Docker copy set.
  const files = collectManagerInputFiles(WEB_ROOT);
  for (const file of files) {
    const rel = path.relative(WEB_ROOT, file).split(path.sep).join("/");
    const covered =
      PRODUCTION_TOP_LEVEL_FILES.includes(rel) ||
      rel.startsWith("src/") ||
      rel.startsWith("public/");
    assert.ok(covered, `local input ${rel} must be a Docker production input`);
  }
});

test("missing canonical product metadata fails instead of falling back", () => {
  const viteConfig = fs.readFileSync(
    path.join(WEB_ROOT, "vite.config.ts"),
    "utf8",
  );
  assert.ok(!viteConfig.includes("0.1.0"));
  const helper = fs.readFileSync(
    path.join(WEB_ROOT, "manager-release.mjs"),
    "utf8",
  );
  assert.ok(!helper.includes("0.1.0"));
  assert.equal(
    parseProductVersionFromPyprojectText('version = "1.2.3"\n'),
    "1.2.3",
  );
  assert.throws(() =>
    parseProductVersionFromPyprojectText('[project]\nname = "x"\n'),
  );
  assert.throws(() =>
    parseProductVersionFromPyprojectText('version = "bad!!version"\n'),
  );
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "wb-manager-noversion-"));
  try {
    assert.throws(() => readProductVersion(tmp));
    assert.throws(() => computeManagerRelease(tmp));
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
  // The checked-in canonical metadata resolves and validates.
  assert.equal(readProductVersion(WEB_ROOT), "0.1.0");
  assert.deepEqual(
    validateManagerRelease(computeManagerRelease(WEB_ROOT)),
    computeManagerRelease(WEB_ROOT),
  );
});

test("Manager client-side validation is bounded and strict", () => {
  assert.deepEqual(tryValidateManagerRelease(validIdentity()), validIdentity());
  assert.equal(tryValidateManagerRelease(null), null);
  assert.equal(tryValidateManagerRelease({}), null);
  assert.equal(
    tryValidateManagerRelease({ ...validIdentity(), extra: 1 }),
    null,
  );
  assert.equal(
    tryValidateManagerRelease({ ...validIdentity(), contract: 2 }),
    null,
  );
  assert.equal(
    tryValidateManagerRelease({ ...validIdentity(), product: "other" }),
    null,
  );
  assert.equal(
    tryValidateManagerRelease({ ...validIdentity(), component: "bridge" }),
    null,
  );
  assert.equal(
    tryValidateManagerRelease({ ...validIdentity(), product_version: "bad!!" }),
    null,
  );
  assert.equal(
    tryValidateManagerRelease({
      ...validIdentity(),
      component_version: "x".repeat(40),
    }),
    null,
  );
  assert.equal(
    tryValidateManagerRelease({ ...validIdentity(), build_id: "sha256:xyz" }),
    null,
  );
  assert.equal(
    tryValidateManagerRelease({
      ...validIdentity(),
      build_id: `sha256:${"A".repeat(64)}`,
    }),
    null,
  );
});

test("full-identity mismatch warns on version skew with equal build ID", () => {
  const compiled = validIdentity();
  assert.equal(describeManagerIdentity(compiled, { ...compiled }), "match");
  assert.equal(
    describeManagerIdentity(compiled, {
      ...compiled,
      product_version: "0.1.1",
      component_version: "0.1.1",
    }),
    "mismatch",
  );
  assert.equal(
    describeManagerIdentity(compiled, {
      ...compiled,
      component_version: "0.1.1",
    }),
    "mismatch",
  );
  assert.equal(
    describeManagerIdentity(compiled, {
      ...compiled,
      build_id: `sha256:${"b".repeat(64)}`,
    }),
    "mismatch",
  );
});

test("invalid or missing compiled identity is unavailable, not a match", () => {
  const served = validIdentity();
  assert.equal(describeManagerIdentity(null, served), "unavailable");
  assert.equal(describeManagerIdentity(undefined, served), "unavailable");
  assert.equal(
    describeManagerIdentity({ ...served, build_id: "bad" }, served),
    "unavailable",
  );
  assert.equal(
    describeManagerIdentity({ ...served, extra: 1 }, served),
    "unavailable",
  );
  // Served missing/invalid remains unavailable, not mismatch.
  const compiled = validIdentity();
  assert.equal(describeManagerIdentity(compiled, null), "unavailable");
  assert.equal(describeManagerIdentity(compiled, undefined), "unavailable");
  assert.equal(
    describeManagerIdentity(compiled, { ...served, build_id: "bad" }),
    "unavailable",
  );
  assert.equal(describeManagerIdentity(null, null), "unavailable");
});
