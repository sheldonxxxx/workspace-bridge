import type { ReleaseIdentity } from "./api";

// M4.1 Manager compile-time identity validation and cached-vs-served
// comparison. Browser-safe: no Node APIs. Used by the Manager Overview
// surface to decide between match, non-destructive mismatch, and
// unavailable. Served missing/invalid is unavailable, never a false
// mismatch; a missing/invalid compile-time identity is also unavailable
// rather than silently treated as matching.

export const MANAGER_CONTRACT = 1;
export const MANAGER_PRODUCT = "workspace-bridge";
export const MANAGER_COMPONENT = "manager";

const VERSION_RE = /^[0-9A-Za-z][0-9A-Za-z._-]{0,31}$/;
const BUILD_ID_RE = /^sha256:[0-9a-f]{64}$/;

const RELEASE_KEYS = [
  "contract",
  "product",
  "product_version",
  "component",
  "component_version",
  "build_id",
] as const;

export type ManagerIdentityStatus = "match" | "mismatch" | "unavailable";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function tryValidateManagerRelease(
  value: unknown,
): ReleaseIdentity | null {
  if (!isRecord(value)) return null;
  const keys = Object.keys(value);
  if (keys.length !== RELEASE_KEYS.length) return null;
  for (const key of RELEASE_KEYS) {
    if (!Object.prototype.hasOwnProperty.call(value, key)) return null;
  }
  if (value.contract !== MANAGER_CONTRACT) return null;
  if (value.product !== MANAGER_PRODUCT) return null;
  if (value.component !== MANAGER_COMPONENT) return null;
  if (
    typeof value.product_version !== "string" ||
    !VERSION_RE.test(value.product_version)
  ) {
    return null;
  }
  if (
    typeof value.component_version !== "string" ||
    !VERSION_RE.test(value.component_version)
  ) {
    return null;
  }
  if (typeof value.build_id !== "string" || !BUILD_ID_RE.test(value.build_id)) {
    return null;
  }
  return {
    contract: MANAGER_CONTRACT,
    product: MANAGER_PRODUCT,
    product_version: value.product_version as string,
    component: MANAGER_COMPONENT,
    component_version: value.component_version as string,
    build_id: value.build_id as string,
  };
}

export function describeManagerIdentity(
  compiled: unknown,
  served: unknown,
): ManagerIdentityStatus {
  const compiledRelease = tryValidateManagerRelease(compiled);
  const servedRelease = tryValidateManagerRelease(served);
  // Served missing/invalid is unavailable, not a mismatch. A
  // missing/invalid compile-time identity while the server reports a
  // Manager identity is also unavailable rather than a silent match.
  if (!compiledRelease || !servedRelease) return "unavailable";
  // Full validated release identity relevant to compatibility, not
  // build_id alone: an equal build_id with a different product (or
  // component) version still warns.
  if (
    compiledRelease.contract !== servedRelease.contract ||
    compiledRelease.product !== servedRelease.product ||
    compiledRelease.product_version !== servedRelease.product_version ||
    compiledRelease.component !== servedRelease.component ||
    compiledRelease.component_version !== servedRelease.component_version ||
    compiledRelease.build_id !== servedRelease.build_id
  ) {
    return "mismatch";
  }
  return "match";
}
