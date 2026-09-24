export type Json = Record<string, unknown>;

export interface RuntimeGrant {
  enabled: boolean;
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
  profile?: { id: string } | null;
}
export interface Workspace {
  id: string;
  name: string;
  root: string;
  enabled: boolean;
  agent_enabled: boolean;
  write_scope: "none" | "handoff" | "workspace";
  excludes: string;
  runtime_grants: Record<string, RuntimeGrant>;
}
export interface RuntimeInfo {
  configured?: boolean;
  healthy?: boolean;
  locked?: boolean;
  protocol?: number;
  version?: string;
  native_version?: string;
  adapter_version?: string;
  detail?: string;
}
export interface ModelPolicy {
  configured?: boolean;
  enabled?: string[];
  default?: string | null;
  reasoning_defaults?: Record<string, string>;
  enabled_count?: number;
}
export interface Status {
  version: string;
  listen_mode: string;
  allowed_parents: string[];
  mcp_port: number;
  bridge: { configured: boolean; enabled: boolean };
  runtimes?: { runtimes: Record<string, RuntimeInfo> };
  runtime_policies?: Record<string, ModelPolicy>;
}
export interface Run {
  run_id: string;
  runtime?: string;
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
  created?: string;
  started?: string;
  finished?: string;
  duration_seconds?: number;
  conversation_id?: string;
  updated?: string;
  continue_from_run_id?: string;
  parent_run_id?: string;
  interactions?: Interaction[];
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
}
export interface Model {
  selector: string;
  displayName?: string;
  name?: string;
  model?: string;
  reasoningOptions?: string[];
  defaultReasoningEffort?: string | null;
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
  return id === "pi" ? "Pi" : id === "codex" ? "Codex" : id;
}
export function jsonText(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}
