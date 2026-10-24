import type { Json, Run } from "@/lib/api";

export type ExecutionRecord = {
  execution_id: string;
  sequence?: number;
  tool?: string;
  state?: string;
  target_preview?: string;
  input_preview?: string;
  output_preview?: string;
  output_truncated?: boolean;
  started?: string;
  duration_ms?: number;
  is_error?: boolean;
  truncated?: boolean;
};
export type RunActivity = {
  id: string;
  kind: string;
  status: string;
  created?: string;
  details?: Json;
};
export type FeedCursor = { created: string; id: string };
export type ActivityPage = {
  activities: RunActivity[];
  next_cursor: FeedCursor | null;
};
export type ExecutionPage = {
  executions: ExecutionRecord[];
  next_cursor: FeedCursor | null;
};

async function read<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, { credentials: "same-origin", signal });
  const data = (await response.json().catch(() => ({}))) as T & {
    error?: string;
  };
  if (!response.ok)
    throw new Error(data.error || `Request failed (HTTP ${response.status})`);
  return data;
}

export type RunPage = { runs: Run[]; next_offset: number | null };
export function getRuns(offset = 0, limit = 25, signal?: AbortSignal) {
  return read<RunPage>(`/api/runs?offset=${offset}&limit=${limit}`, signal);
}
export function getWorkspaceRuns(workspaceId: string, signal?: AbortSignal) {
  return read<{ runs: Run[] }>(
    `/api/workspaces/${encodeURIComponent(workspaceId)}/runs`,
    signal,
  );
}
export async function getRunSnapshot(runId: string, signal?: AbortSignal) {
  const path = `/api/runs/${encodeURIComponent(runId)}`;
  const [run, activity, executions] = await Promise.allSettled([
    read<Run>(path, signal),
    read<ActivityPage>(`${path}/activities?limit=50`, signal),
    read<ExecutionPage>(`${path}/executions?limit=50`, signal),
  ]);
  if (run.status === "rejected") throw run.reason;
  return {
    run: run.value,
    activity: activity.status === "fulfilled" ? activity.value : null,
    executions: executions.status === "fulfilled" ? executions.value : null,
  };
}
export function getOlderActivities(
  runId: string,
  cursor: FeedCursor,
  signal?: AbortSignal,
) {
  return read<ActivityPage>(feedPath(runId, "activities", cursor), signal);
}
export function getOlderExecutions(
  runId: string,
  cursor: FeedCursor,
  signal?: AbortSignal,
) {
  return read<ExecutionPage>(feedPath(runId, "executions", cursor), signal);
}
function feedPath(runId: string, feed: string, cursor: FeedCursor) {
  const query = new URLSearchParams({
    limit: "50",
    before_created: cursor.created,
    before_id: cursor.id,
  });
  return `/api/runs/${encodeURIComponent(runId)}/${feed}?${query}`;
}

export type RunDiffPage = {
  patch: string;
  offset: number;
  next_offset: number | null;
  total_bytes: number;
  redacted: boolean;
};
export function getRunDiff(
  runId: string,
  path: string | null,
  offset: number,
  signal?: AbortSignal,
) {
  const query = new URLSearchParams({ offset: String(offset) });
  if (path) query.set("path", path);
  return read<RunDiffPage>(
    `/api/runs/${encodeURIComponent(runId)}/diff?${query}`,
    signal,
  );
}
export type CheckpointStats = { checkpoints: number; store_bytes: number };
export function getCheckpointStats(workspaceId: string, signal?: AbortSignal) {
  return read<CheckpointStats>(
    `/api/workspaces/${encodeURIComponent(workspaceId)}/checkpoints`,
    signal,
  );
}
