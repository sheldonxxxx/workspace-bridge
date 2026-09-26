import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";

const WEB_ROOT = path.resolve(import.meta.dirname, "..");
const APP = fs.readFileSync(path.join(WEB_ROOT, "src", "App.tsx"), "utf8");
const API = fs.readFileSync(
  path.join(WEB_ROOT, "src", "lib", "api.ts"),
  "utf8",
);

test("Versions view is registered and read-only", () => {
  assert.ok(APP.includes('"versions"'), "versions section id exists");
  assert.ok(
    APP.includes("System / Versions"),
    "System/Versions title exists",
  );
  assert.ok(
    APP.includes("/api/system/versions"),
    "system versions endpoint is used",
  );
  assert.ok(
    !APP.includes("/api/deployment/"),
    "no deployment API endpoints remain",
  );
  assert.ok(!APP.includes("/v1/deployment/"), "no Node deployment routes");
  // Wording treats compatible skew as routine, never a generic error banner.
  assert.ok(
    APP.includes("Update available · Compatible"),
    "compatible update wording exists",
  );
  assert.ok(
    APP.includes("Unsupported development build"),
    "unsupported-build wording exists",
  );
  assert.ok(
    !APP.includes("legacy identity"),
    "no legacy-identity terminology remains",
  );
  assert.ok(
    APP.includes("No downgrade is offered"),
    "target-mismatch offers no downgrade",
  );
  assert.ok(
    APP.includes("Only routes using this component are affected"),
    "incompatible/unavailable scope wording exists",
  );
  assert.ok(
    !APP.includes("release-metadata-gated"),
    "no migration-gate wording remains",
  );
  assert.ok(
    APP.includes("Informational only"),
    "informational-only affordance wording exists",
  );
});

test("Versions view has manual guidance and no managed-update UI", () => {
  // Manual local-update guidance only.
  assert.ok(APP.includes("uv tool upgrade"), "uv tool upgrade guidance");
  assert.ok(
    APP.includes("workspace-bridge-pi-host-adapter"),
    "Pi npm package is named",
  );
  assert.ok(
    APP.includes("never installs, restarts, or rolls back"),
    "no-mutation statement exists",
  );
  // No managed-updater concepts.
  assert.ok(!APP.includes("managedUpdateEligibility"));
  assert.ok(!APP.includes("Managed update"));
  assert.ok(!APP.includes("Ready for managed update"));
  assert.ok(!APP.includes("selective update actions arrive"));
  assert.ok(!APP.includes("deployment updater"));
  assert.ok(!APP.includes("System / Updates"));
  // No POST/PUT/PATCH/DELETE to system endpoints and no placeholder
  // success. The view is read-only.
  const systemPosts = [
    ...APP.matchAll(
      /api\(\s*[`'"]\/api\/system[^`'"]*[`'"]\s*,\s*[`'"](POST|PUT|PATCH|DELETE)[`'"]/g,
    ),
  ];
  assert.equal(systemPosts.length, 0);
  // No fake success toast for updates.
  assert.ok(!APP.includes("Update started"));
  assert.ok(!APP.includes("Update complete"));
  // Target version is shown prominently.
  assert.ok(APP.includes("Target Bridge"));
});

test("Versions status types separate compatibility from update state", () => {
  assert.ok(API.includes("VersionComponentState"));
  assert.ok(API.includes("VersionStatus"));
  assert.ok(API.includes("update_available"));
  assert.ok(API.includes("unsupported_build"));
  assert.ok(!API.includes("legacy_update_available"));
  assert.ok(API.includes("target_mismatch"));
  assert.ok(API.includes("execution_compatible"));
  assert.ok(API.includes("target_precision"));
  assert.ok(!API.includes("managedUpdateEligibility"));
  assert.ok(!API.includes("SelectivePlan"));
  assert.ok(!API.includes("DeploymentStatus"));
});

test("Built Manager contains the Versions view", () => {
  const dist = path.resolve(
    WEB_ROOT,
    "..",
    "workspace_bridge",
    "static",
    "dist",
    "assets",
  );
  const files = fs.readdirSync(dist).filter((f) => f.endsWith(".js"));
  assert.ok(files.length > 0);
  const combined = files
    .map((f) => fs.readFileSync(path.join(dist, f), "utf8"))
    .join("\n");
  assert.ok(combined.includes("System / Versions"));
  assert.ok(combined.includes("Update available"));
  assert.ok(!combined.includes("System / Updates"));
  assert.ok(!combined.includes("/api/deployment/status"));
});
