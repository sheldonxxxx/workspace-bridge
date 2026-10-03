export type Json = Record<string, unknown>;

export interface WorkspaceRoute {
  adapter_id: string;
  name: string;
  runtime_type: "pi" | "codex" | "claude";
  node_id?: string;
  node_name?: string;
  adapter_enabled: boolean;
  enabled: boolean;
  is_default?: boolean;
  readiness?: string;
  ready?: boolean;
  default_model?: string | null;
  security_binding?: {
    source: "profile" | "runtime-config";
    profile?: { id: string; revision?: string };
    revision?: string;
    status?: string;
    observed_revision?: string | null;
    resolved_summary?: {
      activePermissionProfile?: string | null;
      approvalPolicy?: string;
      approvalsReviewer?: string;
      provenance?: string;
    } | null;
  } | null;
  profile?: { id: string; revision?: string } | null;
  effective_security?: {
    source?: string;
    profile_id?: string | null;
    bound_revision?: string | null;
    observed_revision?: string | null;
    freshness?: string;
    status?: string;
    resolved_summary?: {
      activePermissionProfile?: string | null;
      approvalPolicy?: string;
      approvalsReviewer?: string;
      provenance?: string;
    } | null;
  } | null;
}
export interface AvailableAdapter {
  adapter_id: string;
  name: string;
  runtime_type: "pi" | "codex" | "claude";
  enabled: boolean;
  node_id: string;
  route_enabled?: boolean;
  readiness?: string;
}
export interface Workspace {
  id: string;
  name: string;
  root: string;
  enabled: boolean;
  agent_enabled?: boolean;
  write_scope: "none" | "handoff" | "workspace";
  excludes: string;
  node_id: string;
  node_name: string;
  node_enabled?: boolean;
  node_revision?: string;
  routes: Record<string, WorkspaceRoute>;
  available_adapters?: AvailableAdapter[];
  node_adapter_count?: number;
}
export interface ReleaseIdentity {
  contract: number;
  product: string;
  product_version: string;
  component: string;
  component_version: string;
  build_id: string;
}
export interface NodeInfo {
  id: string;
  name: string;
  base_url: string;
  enabled: boolean;
  revision: string;
  has_token: boolean;
  health?: string;
  protocol?: number;
  node_version?: string;
  capabilities?: string[];
  allowed_root_count?: number;
  release?: ReleaseIdentity | null;
}
export interface AdapterInfo {
  id: string;
  node_id?: string;
  node_name?: string;
  name: string;
  runtime_type: "pi" | "codex" | "claude";
  base_url: string;
  enabled: boolean;
  revision: string;
  has_token: boolean;
  configured?: boolean;
  healthy?: boolean;
  locked?: boolean;
  protocol?: number;
  version?: string;
  native_version?: string;
  adapter_version?: string;
  detail?: string;
  release?: ReleaseIdentity | null;
  model_policy?: ModelPolicy;
  features?: Record<string, number>;
}
export interface ModelPolicy {
  configured?: boolean;
  enabled?: string[];
  default?: string | null;
  reasoning_defaults?: Record<string, string>;
  enabled_count?: number;
}
export interface UsageLimitWindow {
  usedPercent: number;
  remainingPercent: number;
  windowDurationMins?: number | null;
  resetsAt?: number | null;
}
export interface UsageLimitBucket {
  limitId?: string | null;
  limitName?: string | null;
  planType?: string | null;
  rateLimitReachedType?: string | null;
  spendControlReached?: boolean | null;
  credits?: {
    hasCredits: boolean;
    unlimited: boolean;
    balance?: string | null;
  } | null;
  individualLimit?: {
    limit: string;
    used: string;
    remainingPercent: number;
    resetsAt?: number | null;
  } | null;
  /** Normalized primary/secondary windows; remaining quota needs one. */
  windows: UsageLimitWindow[];
}
export interface UsageLimits {
  available: boolean;
  ordinaryUsageAllowed?: boolean | null;
  buckets: UsageLimitBucket[];
}
export function quotaWindowLabel(mins?: number | null): string {
  if (typeof mins !== "number" || !Number.isFinite(mins) || mins <= 0)
    return "";
  if (mins % 1440 === 0) return `${mins / 1440}d`;
  if (mins % 10080 === 0) return `${mins / 10080}w`;
  if (mins % 60 === 0) return `${mins / 60}h`;
  return `${mins}m`;
}
export function quotaResetText(resetsAt?: number | null): string {
  if (
    typeof resetsAt !== "number" ||
    !Number.isFinite(resetsAt) ||
    resetsAt <= 0
  )
    return "";
  const date = new Date(resetsAt * 1000);
  if (Number.isNaN(date.getTime())) return "";
  return `resets ${date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  })}`;
}
export interface Status {
  version: string;
  release?: ReleaseIdentity | null;
  manager_release?: ReleaseIdentity | null;
  listen_mode: string;
  mcp_port: number;
  bridge: { configured: boolean; enabled: boolean };
  adapters?: { adapters: AdapterInfo[] };
  nodes?: NodeInfo[];
}
export type DiagnosticStatus =
  "pass" | "warning" | "unknown" | "action_required" | "failed";
export interface DiagnosticCheck {
  id: string;
  code: string;
  section: string;
  status: DiagnosticStatus;
  summary: string;
  detail?: string;
  remediation?: string;
  workspace_id?: string;
  adapter_id?: string;
  runtime_type?: string;
}
export interface RunnableRoute {
  id: string;
  workspace_id: string;
  workspace_name: string;
  adapter_id: string;
  adapter_name: string;
  runtime_type: string;
  node_id?: string;
  node_name?: string;
  ready: boolean;
  status: string;
  summary: string;
  blockers: string[];
  is_default?: boolean;
  profile?: { id: string; revision?: string } | null;
  profile_revision?: string | null;
  security_source?: string | null;
  default_model_selector?: string | null;
}
export interface DiagnosticReport {
  generated_at: string;
  mode: string;
  overall: {
    status: DiagnosticStatus;
    summary: string;
    counts: Record<DiagnosticStatus, number>;
  };
  checks: DiagnosticCheck[];
  runnable_routes: RunnableRoute[];
}
export interface TokenUsage {
  input_tokens?: number;
  cached_input_tokens?: number;
  cache_write_input_tokens?: number;
  output_tokens?: number;
  reasoning_output_tokens?: number;
  total_tokens?: number;
}
export interface Run {
  run_id: string;
  adapter_id?: string;
  adapter_name?: string;
  node_id?: string;
  node_name?: string;
  node_revision?: string;
  adapter_revision?: string;
  runtime_type?: string;
  phase?: string;
  active_state?: string;
  outcome?: string;
  state?: string;
  active?: boolean;
  job_id?: string;
  handoff_title?: string;
  workspace_id?: string;
  workspace_name?: string;
  model?: string;
  reasoning?: string | null;
  token_usage?: TokenUsage | null;
  created?: string;
  started?: string;
  finished?: string;
  duration_seconds?: number;
  conversation_id?: string;
  updated?: string;
  continue_from_run_id?: string;
  parent_run_id?: string;
  interactions?: Interaction[];
  effective_security?: {
    source?: string;
    profile_id?: string | null;
    bound_revision?: string | null;
    effective_revision?: string | null;
    resolved_summary?: Record<string, unknown>;
  };
}
export interface Interaction {
  id: string;
  kind: string;
  state: string;
  details?: {
    title?: string;
    resource?: string;
    requested?: Json;
    choices?: Array<{ id: string; label: string; semantic?: string }>;
    fields?: Array<{
      id: string;
      question?: string;
      header?: string;
      options?: Array<{ label: string }>;
    }>;
  };
}
export interface Handoff {
  id: string;
  title: string;
  state: string;
  path: string;
  copy_prompt: string;
}
export interface Event {
  at: string;
  workspace?: string;
  action: string;
  outcome: string;
  node_id?: string;
  node_name?: string;
  adapter_id?: string;
  adapter_name?: string;
  runtime_type?: string;
}
export interface Model {
  selector: string;
  displayName?: string;
  name?: string;
  model?: string;
  reasoningOptions?: string[];
  defaultReasoningEffort?: string | null;
}

export interface VersionComponentState {
  component: string;
  instance: string;
  runtime_type?: string | null;
  instance_id?: string | null;
  state:
    | "current"
    | "update_available"
    | "unsupported_build"
    | "target_mismatch"
    | "incompatible"
    | "unavailable";
  execution_compatible: boolean;
  reason: string;
  current_product_version?: string | null;
  current_build_id?: string | null;
  target_product_version?: string | null;
  target_build_id?: string | null;
  target_precision: "exact" | "product-version-only";
  current?: ReleaseIdentity | null;
}
export interface VersionStatus {
  schema_version: number;
  status: "ok" | "failed";
  target: {
    product_version?: string | null;
    build_id?: string | null;
    manager?: ReleaseIdentity | null;
    note?: string;
  };
  bridge?: VersionComponentState | null;
  manager?: VersionComponentState | null;
  nodes?: VersionComponentState[];
  adapters?: VersionComponentState[];
  error?: { code: string; summary: string };
}
export async function api<T>(
  path: string,
  method = "GET",
  body?: unknown,
): Promise<T> {
  const response = await fetch(path, {
    method,
    credentials: "same-origin",
    headers:
      body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = (await response.json().catch(() => ({}))) as T & {
    error?: string;
  };
  if (!response.ok)
    throw new Error(data.error || `Request failed (HTTP ${response.status})`);
  return data;
}
export function displayState(run: Run): string {
  const value =
    run.phase === "terminal"
      ? run.outcome
      : run.active_state || run.phase || run.state;
  return (value || "Unknown").replaceAll("_", " ");
}
export function dateTime(value?: string): string {
  return value ? new Date(value).toLocaleString() : "—";
}
export function runtimeName(id: string): string {
  return id === "pi"
    ? "Pi"
    : id === "codex"
      ? "Codex"
      : id === "claude"
        ? "Claude Code"
        : id;
}
export function adapterName(
  adapter?: Pick<AdapterInfo, "name" | "runtime_type">,
): string {
  return adapter
    ? `${adapter.name} · ${runtimeName(adapter.runtime_type)}`
    : "Unknown adapter";
}
export function jsonText(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}
