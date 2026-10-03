import assert from "node:assert/strict";
import { test } from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { canonicalizeProjectsDir, resolveSessionDir, PathError } from "../paths.mjs";

function makeTree() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "pi-paths-"));
  const projects = path.join(root, "Projects");
  const app = path.join(projects, "my-app");
  fs.mkdirSync(app, { recursive: true });
  return { root, projects, app };
}

test("canonicalizeProjectsDir requires an existing directory", () => {
  assert.throws(() => canonicalizeProjectsDir(""), PathError);
  assert.throws(() => canonicalizeProjectsDir(path.join(os.tmpdir(), "no-such-dir-xyz")), PathError);
  const { projects } = makeTree();
  assert.equal(canonicalizeProjectsDir(projects), fs.realpathSync(projects));
});

test("canonicalizeProjectsDir rejects files and filesystem root", () => {
  const { projects } = makeTree();
  const file = path.join(projects, "f.txt");
  fs.writeFileSync(file, "x");
  assert.throws(() => canonicalizeProjectsDir(file), PathError);
  assert.throws(() => canonicalizeProjectsDir(path.parse(projects).root), PathError);
});

test("resolveSessionDir accepts children and nested children", () => {
  const { projects, app } = makeTree();
  const root = canonicalizeProjectsDir(projects);
  assert.equal(resolveSessionDir(root, app), fs.realpathSync(app));
  const nested = path.join(app, "sub");
  fs.mkdirSync(nested);
  assert.equal(resolveSessionDir(root, nested), fs.realpathSync(nested));
});

test("resolveSessionDir rejects root, home, outside, missing, and files", () => {
  const { projects, app } = makeTree();
  const root = canonicalizeProjectsDir(projects);
  assert.throws(() => resolveSessionDir(root, ""), PathError);
  assert.throws(() => resolveSessionDir(root, path.parse(app).root), PathError);
  assert.throws(() => resolveSessionDir(root, os.homedir()), PathError);
  assert.throws(() => resolveSessionDir(root, "~"), PathError);
  assert.throws(() => resolveSessionDir(root, "~/x"), PathError);
  assert.throws(() => resolveSessionDir(root, os.tmpdir()), PathError);
  assert.throws(() => resolveSessionDir(root, path.join(app, "missing")), PathError);
  assert.throws(() => resolveSessionDir(root, root), PathError);
  const file = path.join(app, "f.txt");
  fs.writeFileSync(file, "x");
  assert.throws(() => resolveSessionDir(root, file), PathError);
});

test("resolveSessionDir rejects symlink escapes", () => {
  const { root, projects, app } = makeTree();
  const rootCanon = canonicalizeProjectsDir(projects);
  const outside = path.join(root, "outside");
  fs.mkdirSync(outside);
  const link = path.join(app, "evil-link");
  fs.symlinkSync(outside, link);
  assert.throws(() => resolveSessionDir(rootCanon, link), PathError);
  assert.throws(() => resolveSessionDir(rootCanon, path.join(link, "sub")), PathError);
});
