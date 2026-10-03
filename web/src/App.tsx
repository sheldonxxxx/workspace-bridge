import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import {
  Activity,
  ArrowRight,
  Check,
  ChevronDown,
  CircleHelp,
  Clipboard,
  Command,
  FolderClosed,
  GitBranch,
  LockKeyhole,
  Menu,
  Moon,
  Pause,
  Play,
  Plus,
  RefreshCw,
  Search,
  Server,
  Settings2,
  Shield,
  SquareTerminal,
  Sun,
  Workflow,
  X,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import {
  api,
  dateTime,
  displayState,
  jsonText,
  runtimeName,
  type VersionComponentState,
  type VersionStatus,
  type DiagnosticCheck,
  type DiagnosticReport,
  type AdapterInfo,
  type Event,
  type Handoff,
  type Json,
  type Model,
  type ModelPolicy,
  type NodeInfo,
  type Run,
  type RunnableRoute,
  type Status,
  type UsageLimits,
  type Workspace,
  quotaResetText,
  quotaWindowLabel,
} from "@/lib/api";
import "./app.css";
import { ProfileManager } from "./ProfileEditor";
import { ProfileAssignment } from "./ProfileAssignment";
import { CommandPalette, type PaletteCommand } from "./CommandPalette";
import { describeManagerIdentity } from "@/lib/manager-identity";

function compiledManagerReleaseRaw(): unknown {
  try {
    return typeof __MANAGER_RELEASE__ !== "undefined"
      ? (__MANAGER_RELEASE__ as unknown)
      : null;
  } catch {
    return null;
  }
}

function shortBuildId(buildId?: string | null): string {
  if (typeof buildId === "string" && /^sha256:[0-9a-f]{64}$/.test(buildId)) {
    return `sha256:${buildId.slice(7, 19)}…`;
  }
  return "unknown";
}

function versionStateLabel(state: VersionComponentState["state"]): string {
  if (state === "current") return "Current";
  if (state === "update_available") return "Update available · Compatible";
  if (state === "unsupported_build") return "Unsupported development build";
  if (state === "target_mismatch") return "Newer than target · No action";
  if (state === "incompatible") return "Incompatible · Affected routes only";
  return "Unavailable · Affected routes only";
}

function versionStateDetail(entry: VersionComponentState): string {
  if (entry.state === "current") return "Matches the target. No action needed.";
  if (entry.state === "update_available")
    return "Existing routes stay runnable. Update manually on the host when convenient.";
  if (entry.state === "unsupported_build") {
    if (entry.reason === "release-invalid")
      return "Invalid release identity. Reinstall a supported release manually; protocol-compatible routes may still run.";
    if (entry.reason === "release-unsupported")
      return "Unsupported release contract. Reinstall a supported release manually; protocol-compatible routes may still run.";
    return "Release identity is missing. Reinstall a supported release manually; protocol-compatible routes may still run.";
  }
  if (entry.state === "target_mismatch")
    return "Newer than the target. No downgrade is offered; compatible routes stay runnable.";
  if (entry.state === "incompatible")
    return "Bridge protocol mismatch. Only routes using this component are affected.";
  return "Unavailable or disabled. Only routes using this component are affected.";
}

function VersionRow({
  entry,
  displayName,
}: {
  entry: VersionComponentState;
  displayName?: string;
}) {
  const name =
    entry.component === "bridge"
      ? "Bridge"
      : entry.component === "manager"
        ? "Manager"
        : displayName || entry.instance;
  const packageName =
    entry.component === "manager"
      ? null
      : entry.runtime_type === "pi"
        ? "workspace-bridge-pi-host-adapter"
        : entry.runtime_type === "claude"
          ? "workspace-bridge[claude]"
          : "workspace-bridge";
  return (
    <div className="version-row">
      <div className="version-identity">
        <h4>{name}</h4>
        {entry.runtime_type && <small>{runtimeName(entry.runtime_type)}</small>}
      </div>
      <div className="version-value">
        <span>Installed</span>
        <strong>{entry.current_product_version || "Unknown"}</strong>
      </div>
      <div className="version-value">
        <span>Target</span>
        <strong>{entry.target_product_version || "Unknown"}</strong>
      </div>
      <div className="version-status">
        <span className={`version-status-badge version-status-${entry.state}`}>
          {versionStateLabel(entry.state)}
        </span>
        <p>{versionStateDetail(entry)}</p>
      </div>
      <details className="version-details">
        <summary>Build and package details</summary>
        <dl>
          <div>
            <dt>Installed build</dt>
            <dd>
              <code>{entry.current_build_id || "Unknown"}</code>
            </dd>
          </div>
          <div>
            <dt>Target build</dt>
            <dd>
              {entry.target_precision === "product-version-only" ? (
                "Compared by version only"
              ) : (
                <code>{entry.target_build_id || "Unknown"}</code>
              )}
            </dd>
          </div>
          {packageName && (
            <div>
              <dt>Package</dt>
              <dd>
                <code>{packageName}</code> (
                {entry.runtime_type === "pi" ? "npm" : "PyPI / uv tool"})
              </dd>
            </div>
          )}
          {displayName && displayName !== entry.instance && (
            <div>
              <dt>Instance ID</dt>
              <dd>{entry.instance}</dd>
            </div>
          )}
        </dl>
      </details>
    </div>
  );
}

function VersionGroup({
  title,
  entries,
  empty,
  names = {},
}: {
  title: string;
  entries: VersionComponentState[];
  empty: string;
  names?: Record<string, string>;
}) {
  return (
    <section className="version-group">
      <h3>{title}</h3>
      {entries.length ? (
        <div className="version-list">
          {entries.map((entry) => (
            <VersionRow
              key={`${entry.component}:${entry.instance}`}
              entry={entry}
              displayName={names[entry.instance]}
            />
          ))}
        </div>
      ) : (
        <p className="version-empty">{empty}</p>
      )}
    </section>
  );
}

type Section =
  | "overview"
  | "nodes"
  | "workspaces"
  | "adapters"
  | "handoffs"
  | "runs"
  | "versions"
  | "audit";
type ConfirmState = {
  title: string;
  description: string;
  action: () => Promise<void>;
  destructive?: boolean;
};
type TextDetail = { title: string; description?: string; content: unknown };
const sections: Array<{
  id: Section;
  title: string;
  icon: typeof Activity;
  description: string;
}> = [
  {
    id: "overview",
    title: "Overview",
    icon: Activity,
    description: "What is ready and what needs attention",
  },
  {
    id: "nodes",
    title: "Nodes",
    icon: Server,
    description:
      "Each Node is the authority for a machine-local workspace root, Git evidence, handoffs, and runtime adapters",
  },
  {
    id: "workspaces",
    title: "Workspaces",
    icon: FolderClosed,
    description: "Access to your projects",
  },
  {
    id: "adapters",
    title: "Adapters",
    icon: Command,
    description:
      "Runtime execution destinations with model policy and security profiles",
  },
  {
    id: "handoffs",
    title: "Handoffs",
    icon: GitBranch,
    description: "Prepared work by workspace",
  },
  {
    id: "runs",
    title: "Runs",
    icon: Play,
    description: "Agent progress and live requests",
  },
  {
    id: "versions",
    title: "System / Versions",
    icon: RefreshCw,
    description:
      "Component versions and compatibility; updates are manual and local",
  },
  {
    id: "audit",
    title: "Audit",
    icon: Shield,
    description: "Access and configuration history",
  },
];
const pageSize = 25;
const autoRefreshMs = 30000;
// Codex account quota is read only while the Adapters section is visible and
// refreshed at most once per adapter per minute; never from /api/status.
const quotaRefreshMs = 60000;
function initialSection(): Section {
  const value = location.hash.slice(1);
  return sections.some((s) => s.id === value) ? (value as Section) : "overview";
}
function initialTheme(): "light" | "dark" {
  try {
    const stored = localStorage.getItem("wb-theme");
    if (stored === "light" || stored === "dark") return stored;
  } catch {
    /* storage unavailable */
  }
  // The console is designed dark-first; light stays one toggle away.
  return "dark";
}
function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return (
    target.isContentEditable ||
    /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName) ||
    Boolean(target.closest("[role=dialog],[role=alertdialog]"))
  );
}
function relativeTime(then: number, now: number): string {
  const seconds = Math.max(0, Math.round((now - then) / 1000));
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  return `${Math.floor(minutes / 60)}h ago`;
}
function StateBadge({ value }: { value: string }) {
  const lower = value.toLowerCase();
  const tone = /healthy|ready|completed|succeeded|enabled|active/.test(lower)
    ? "success"
    : /waiting|review|pending|paused/.test(lower)
      ? "warning"
      : /failed|error|unavailable|locked|blocked/.test(lower)
        ? "danger"
        : "neutral";
  return (
    <Badge variant="outline" className={`state-badge tone-${tone}`}>
      {value}
    </Badge>
  );
}
function routeChecks(
  report: DiagnosticReport | null,
  route: RunnableRoute,
): Array<{ code: string; check?: DiagnosticCheck }> {
  return route.blockers.map((code) => ({
    code,
    check: report?.checks.find(
      (check) =>
        check.code === code &&
        (!check.workspace_id || check.workspace_id === route.workspace_id) &&
        (!check.adapter_id || check.adapter_id === route.adapter_id),
    ),
  }));
}
function diagnosticDestination(
  check?: DiagnosticCheck,
  code?: string,
): Section {
  const value = code || check?.code || "";
  if (check?.section === "workspaces" || value.startsWith("workspace."))
    return "workspaces";
  if (value.startsWith("profile.") || value.startsWith("adapter."))
    return "adapters";
  return "adapters";
}
function securityDetail(
  grant: Workspace["routes"][string] | undefined,
): string {
  const effective = grant?.effective_security;
  if (!effective)
    return "Unavailable — route cannot run until security resolves.";
  if (effective.source === "profile") {
    return `Profile ${effective.profile_id || "unknown"} · revision ${(effective.bound_revision || "unknown").slice(0, 12)} · ${effective.freshness || "unknown"}`;
  }
  const summary = effective.resolved_summary || {};
  return `Codex runtime config · ${String(summary.activePermissionProfile || "permission profile unknown")} · approval ${String(summary.approvalPolicy || "unknown")} · reviewer ${String(summary.approvalsReviewer || "unknown")} · ${effective.status || "unavailable"}`;
}
function isDiagnosticReport(value: unknown): value is DiagnosticReport {
  if (!value || typeof value !== "object") return false;
  const report = value as Partial<DiagnosticReport>;
  return Boolean(
    typeof report.generated_at === "string" &&
    typeof report.mode === "string" &&
    Array.isArray(report.checks) &&
    Array.isArray(report.runnable_routes) &&
    report.overall &&
    typeof report.overall.status === "string" &&
    typeof report.overall.summary === "string" &&
    report.overall.counts &&
    report.runnable_routes.every(
      (route) =>
        typeof route.workspace_id === "string" &&
        typeof route.adapter_id === "string" &&
        typeof route.ready === "boolean",
    ),
  );
}
function RouteSummary({
  route,
  report,
  unavailable,
  security,
  onNavigate,
  compact = false,
}: {
  route: RunnableRoute;
  report: DiagnosticReport | null;
  unavailable: boolean;
  security: string;
  onNavigate: (section: Section) => void;
  compact?: boolean;
}) {
  const blockers = routeChecks(report, route);
  const visibleBlockers = compact ? blockers.slice(0, 1) : blockers;
  return (
    <article
      className={`diagnostic-route ${compact ? "diagnostic-route-compact" : ""}`}
    >
      <div className="diagnostic-route-head">
        <div>
          <strong>{route.workspace_name}</strong>
          <span>
            {route.adapter_name} · {runtimeName(route.runtime_type)}
          </span>
        </div>
        <div className="row-actions">
          {route.is_default && <StateBadge value="Workspace default" />}
          <StateBadge
            value={
              unavailable
                ? "Diagnostics unavailable"
                : route.ready
                  ? "Ready"
                  : "Blocked"
            }
          />
        </div>
      </div>
      <p className="diagnostic-route-facts">
        <span>Node: {route.node_name || route.node_id || "Not reported"}</span>
        <span>
          Default model: {route.default_model_selector || "Not reported"}
        </span>
        <span>Security: {security}</span>
        <span>Blockers: {route.blockers.length}</span>
      </p>
      {unavailable ? (
        <p className="diagnostic-route-message">
          The last route snapshot is stale. Refresh diagnostics before starting.
        </p>
      ) : !route.ready ? (
        <div className="diagnostic-blockers">
          {visibleBlockers.length ? (
            visibleBlockers.map(({ code, check }) => (
              <div className="diagnostic-blocker" key={`${route.id}:${code}`}>
                <p>{check?.summary || `Blocked by ${code}.`}</p>
                {check?.remediation && <small>{check.remediation}</small>}
                {(!compact || check?.remediation) && (
                  <Button
                    variant="link"
                    size="sm"
                    className="inline-action"
                    onClick={() =>
                      onNavigate(diagnosticDestination(check, code))
                    }
                  >
                    {check?.remediation ? "Resolve" : "Open"}
                  </Button>
                )}
              </div>
            ))
          ) : (
            <p className="diagnostic-route-message">{route.summary}</p>
          )}
        </div>
      ) : null}
    </article>
  );
}
function Empty({
  title,
  children,
  action,
}: {
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="empty-view">
      <div className="empty-symbol">
        <CircleHelp size={22} />
      </div>
      <h3>{title}</h3>
      {children && <p>{children}</p>}
      {action}
    </div>
  );
}
function SectionHeading({
  eyebrow,
  title,
  description,
  action,
}: {
  eyebrow?: string;
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="section-heading">
      <div>
        {eyebrow && <span className="section-kicker">{eyebrow}</span>}
        <h2>{title}</h2>
        {description && <p>{description}</p>}
      </div>
      {action && <div className="heading-action">{action}</div>}
    </div>
  );
}
function RunCard({
  run,
  onOpen,
  compact = false,
}: {
  run: Run;
  onOpen: (run: Run) => void;
  compact?: boolean;
}) {
  const attention = run.active_state === "waiting_interaction";
  return (
    <article
      className={`run-row ${attention ? "run-row-attention" : ""} ${compact ? "run-row-compact" : ""}`}
    >
      <div className="run-main">
        <div className="run-state">
          <StateBadge value={attention ? "Needs review" : displayState(run)} />
          <span>
            {run.adapter_name || "Unknown adapter"} ·{" "}
            {runtimeName(run.runtime_type || "unknown")}
          </span>
        </div>
        <h3>{run.handoff_title || run.job_id || "Untitled handoff"}</h3>
        <p>
          {run.workspace_name || run.workspace_id || "Workspace"}{" "}
          <span aria-hidden="true">/</span> {run.model || "Default model"}
        </p>
        <small className="run-route-context">
          Node: {run.node_name || run.node_id || "Unknown"} · security snapshot
          {run.effective_security?.source
            ? ` · ${run.effective_security.source}`
            : " unavailable"}
        </small>
      </div>
      <div className="run-side">
        <span className="run-updated">
          <span>Updated</span>
          <time dateTime={run.updated || run.created}>
            {dateTime(run.updated || run.created)}
          </time>
        </span>
        <Button
          variant={attention ? "default" : "outline"}
          size="sm"
          onClick={() => onOpen(run)}
        >
          {attention ? "Review run" : "Open run"}
          <ArrowRight size={14} />
        </Button>
      </div>
    </article>
  );
}

type ExecutionRecord = {
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
type RunActivity = {
  id: string;
  kind: string;
  status: string;
  created?: string;
  details?: Json;
};
type FeedCursor = { created: string; id: string };
type ActivityPage = {
  activities: RunActivity[];
  next_cursor: FeedCursor | null;
};
type ExecutionPage = {
  executions: ExecutionRecord[];
  next_cursor: FeedCursor | null;
};

function mergeNewest<T>(
  fresh: T[],
  existing: T[],
  key: (item: T) => string,
): T[] {
  const freshKeys = new Set(fresh.map(key));
  return [...fresh, ...existing.filter((item) => !freshKeys.has(key(item)))];
}

function appendOlder<T>(
  existing: T[],
  older: T[],
  key: (item: T) => string,
): T[] {
  const knownKeys = new Set(existing.map(key));
  return [...existing, ...older.filter((item) => !knownKeys.has(key(item)))];
}

function objectValue(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function outputPreviewText(value: unknown, depth = 0): string {
  if (depth > 5 || value == null) return "";
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (depth < 5 && (trimmed.startsWith("{") || trimmed.startsWith("["))) {
      try {
        const parsed = JSON.parse(trimmed) as unknown;
        const readable = outputPreviewText(parsed, depth + 1);
        if (readable) return readable;
      } catch {
        // Keep ordinary command output unchanged when it is not JSON.
      }
    }
    return value;
  }
  if (Array.isArray(value)) {
    return value
      .map((item) => outputPreviewText(item, depth + 1))
      .filter(Boolean)
      .join("\n");
  }
  if (typeof value !== "object") return String(value);

  const record = value as Record<string, unknown>;
  const previewKeys = [
    "output_preview",
    "outputPreview",
    "aggregatedOutput",
    "stdout",
    "output",
    "preview",
    "message",
    "text",
    "status",
    "content",
  ];
  for (const key of previewKeys) {
    if (record[key] !== undefined && record[key] !== value) {
      const preview = outputPreviewText(record[key], depth + 1);
      if (preview) return preview;
    }
  }

  const ignoredKeys = new Set([
    "is_error",
    "isError",
    "truncated",
    "output_bytes",
    "outputBytes",
    "preview_bytes",
    "previewBytes",
    "durationMs",
    "duration_ms",
  ]);
  return Object.entries(record)
    .filter(([key, item]) => !ignoredKeys.has(key) && item !== undefined)
    .map(([key, item]) => {
      const label = key
        .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
        .replace(/_/g, " ")
        .replace(/\b\w/g, (char) => char.toUpperCase());
      const text = outputPreviewText(item, depth + 1);
      return text ? `${label}: ${text}` : "";
    })
    .filter(Boolean)
    .join("\n");
}

function executionInputPreview(
  execution: ExecutionRecord,
  activityDetails?: Json,
): string {
  const input = objectValue(activityDetails?.input);
  const details = objectValue(input.details);
  const filtered = Object.fromEntries(
    Object.entries(details).filter(
      ([key, value]) =>
        !/(?:_sha256|_bytes)$|^truncated$/i.test(key) && value !== undefined,
    ),
  );
  if (typeof filtered.command === "string") {
    const command = `$ ${filtered.command}`;
    return command.length > 1200 ? `${command.slice(0, 1200)}\n…` : command;
  }
  if (Object.keys(filtered).length) {
    const summary = JSON.stringify(filtered, null, 2);
    return summary.length > 1200 ? `${summary.slice(0, 1200)}\n…` : summary;
  }
  if (execution.input_preview) return execution.input_preview;
  return (
    execution.target_preview ||
    (typeof input.summary === "string" ? input.summary : "") ||
    "Input not recorded"
  );
}

function executionOutputPreview(
  activityDetails?: Json,
  execution?: ExecutionRecord,
): {
  text: string;
  truncated: boolean;
} {
  const result = objectValue(activityDetails?.result);
  const preview =
    outputPreviewText(result) || outputPreviewText(execution?.output_preview);
  return {
    text: preview
      ? preview.slice(0, 900)
      : activityDetails
        ? "No output captured"
        : "Output preview unavailable",
    truncated:
      result.truncated === true ||
      execution?.output_truncated === true ||
      preview.length > 900,
  };
}

function ExecutionStatus({
  state,
  isError,
}: {
  state?: string;
  isError?: boolean;
}) {
  const normalized = (state || "recorded").toLowerCase();
  let tone: "danger" | "success" | "warning" | "neutral" = "neutral";
  if (isError || ["failed", "error"].includes(normalized)) tone = "danger";
  else if (["completed", "succeeded", "success"].includes(normalized))
    tone = "success";
  else if (
    [
      "running",
      "queued",
      "starting",
      "declined",
      "interrupted",
      "cancelled",
      "paused",
    ].includes(normalized)
  ) {
    tone = "warning";
  }
  const Icon =
    tone === "danger"
      ? X
      : tone === "success"
        ? Check
        : ["running", "queued", "starting"].includes(normalized)
          ? Activity
          : ["declined", "interrupted", "cancelled", "paused"].includes(
                normalized,
              )
            ? Pause
            : CircleHelp;
  const label = isError
    ? "Failed"
    : normalized === "recorded"
      ? "Recorded"
      : normalized.charAt(0).toUpperCase() + normalized.slice(1);
  return (
    <span className={`execution-status execution-status-${tone}`}>
      <Icon size={13} strokeWidth={2.5} aria-hidden="true" />
      {label}
    </span>
  );
}

function WorkspaceCard({
  ws,
  diagnostics,
  diagnosticsUnavailable,
  onManage,
  onRoute,
  onProfile,
  onHandoffs,
  onNavigate,
  onConfirm,
  onNotice,
  onDetail,
}: {
  ws: Workspace;
  diagnostics: DiagnosticReport | null;
  diagnosticsUnavailable: boolean;
  onManage: (ws: Workspace, operation: string, extra?: Json) => Promise<void>;
  onRoute: (ws: Workspace, adapterId: string, body: Json) => Promise<void>;
  onProfile: (ws: Workspace, runtimeId: string) => void;
  onHandoffs: (ws: Workspace) => void;
  onNavigate: (section: Section) => void;
  onConfirm: (state: ConfirmState) => void;
  onNotice: (value: string) => void;
  onDetail: (detail: TextDetail) => void;
}) {
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [excludes, setExcludes] = useState(() => {
    try {
      return (JSON.parse(ws.excludes) as string[]).join("\n");
    } catch {
      return "";
    }
  });
  const [writeScope, setWriteScope] = useState(ws.write_scope);
  const exclusionPatterns = excludes
    .split("\n")
    .map((pattern) => pattern.trim())
    .filter(Boolean);
  const savedExclusions = (() => {
    try {
      return JSON.parse(ws.excludes) as string[];
    } catch {
      return [];
    }
  })();
  const settingsChanged =
    writeScope !== ws.write_scope ||
    JSON.stringify(exclusionPatterns) !== JSON.stringify(savedExclusions);
  const change = (
    title: string,
    description: string,
    action: () => Promise<void>,
    destructive = false,
  ) => onConfirm({ title, description, action, destructive });
  return (
    <article className="workspace-entry">
      <div className="workspace-identity">
        <div className="workspace-icon">
          <FolderClosed size={19} />
        </div>
        <div className="workspace-name">
          <div className="workspace-name-line">
            <h3>{ws.name}</h3>
            <Button
              variant="ghost"
              size="icon-xs"
              aria-label={`Copy workspace ID for ${ws.name}`}
              title="Copy workspace ID"
              onClick={async () => {
                try {
                  await navigator.clipboard.writeText(ws.id);
                  onNotice("Workspace ID copied.");
                } catch {
                  onDetail({ title: "Workspace ID", content: ws.id });
                }
              }}
            >
              <Clipboard size={14} />
            </Button>
          </div>
          <p className="path-text" title={ws.root}>
            {ws.root}
          </p>
          <p className="workspace-node-label">
            Node: <strong>{ws.node_name || ws.node_id}</strong> · node-local
            root
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={() => onHandoffs(ws)}>
          Handoffs <ArrowRight size={14} />
        </Button>
      </div>
      <div className="workspace-toggles">
        <div className="access-cell">
          <div>
            <strong>Bridge access</strong>
            <p>ChatGPT can browse this project.</p>
          </div>
          <Switch
            aria-label={`Bridge access for ${ws.name}`}
            checked={ws.enabled}
            onCheckedChange={(enabled) =>
              change(
                enabled ? "Enable bridge access?" : "Disable bridge access?",
                enabled
                  ? "Every chat using the shared bridge connection can access this workspace."
                  : "This workspace will no longer be available through the bridge.",
                () => onManage(ws, enabled ? "enable" : "disable"),
                !enabled,
              )
            }
          />
        </div>
      </div>
      <div className="execution-targets">
        <div className="execution-targets-heading">
          <div>
            <strong>Execution targets</strong>
            <p>Every run uses an adapter owned by this workspace’s Node.</p>
          </div>
          <span className="target-count">
            {Object.keys(ws.routes || {}).length} configured
          </span>
        </div>
        {Object.keys(ws.routes || {}).length === 0 && (
          <div className="execution-target-empty">
            <strong>No execution targets configured</strong>
            <p>
              Add a same-Node adapter below, then choose its security and
              readiness before starting a run.
            </p>
          </div>
        )}
        {Object.entries(ws.routes || {}).map(([id, grant]) => {
          const route = diagnostics?.runnable_routes.find(
            (item) => item.workspace_id === ws.id && item.adapter_id === id,
          );
          const blockers = route ? routeChecks(diagnostics, route) : [];
          const status = diagnosticsUnavailable
            ? "Diagnostics unavailable"
            : route
              ? route.ready
                ? "Ready"
                : "Blocked"
              : "Not evaluated";
          return (
            <div className="target-row" key={id}>
              <div className="target-main">
                <div className="target-title-line">
                  <strong>
                    {grant.name} · {runtimeName(grant.runtime_type)}
                  </strong>
                  {grant.is_default && <StateBadge value="Workspace default" />}
                </div>
                <p className="effective-security-detail">
                  {securityDetail(grant)}
                </p>
                <p className="target-facts">
                  <span>
                    Node: {grant.node_name || ws.node_name || ws.node_id}
                  </span>
                  <span>
                    Default model: {grant.default_model || "Native default"}
                  </span>
                  <span>
                    Workspace route: {grant.enabled ? "Enabled" : "Disabled"}
                  </span>
                </p>
                {grant.security_binding?.source === "runtime-config" &&
                  grant.security_binding.status !== "ready" && (
                    <p className="runtime-security-summary">
                      Codex security config is currently unavailable
                    </p>
                  )}
                {!diagnosticsUnavailable && route && !route.ready && (
                  <div className="runtime-route-blocker">
                    <p>{blockers[0]?.check?.summary || route.summary}</p>
                    {blockers[0]?.check?.remediation && (
                      <small>{blockers[0].check.remediation}</small>
                    )}
                    <Button
                      variant="link"
                      size="sm"
                      className="inline-action"
                      onClick={() =>
                        onNavigate(
                          diagnosticDestination(
                            blockers[0]?.check,
                            blockers[0]?.code,
                          ),
                        )
                      }
                    >
                      View blockers
                    </Button>
                  </div>
                )}
                <div className="target-actions">
                  <Button
                    variant="link"
                    size="sm"
                    className="inline-action"
                    onClick={() => onProfile(ws, id)}
                  >
                    Change security
                  </Button>
                  {grant.is_default ? (
                    <Button
                      variant="link"
                      size="sm"
                      className="inline-action"
                      onClick={() =>
                        void onRoute(ws, id, { clear_default: true })
                      }
                    >
                      Clear default
                    </Button>
                  ) : (
                    <Button
                      variant="link"
                      size="sm"
                      className="inline-action"
                      disabled={!grant.enabled || grant.ready === false}
                      onClick={() => void onRoute(ws, id, { is_default: true })}
                    >
                      Set as default
                    </Button>
                  )}
                </div>
              </div>
              <div className="target-side">
                <div className="runtime-route-readiness">
                  <span>Route readiness</span>
                  <StateBadge value={status} />
                </div>
                <Switch
                  aria-label={`${grant.name} route for ${ws.name}`}
                  checked={grant.enabled}
                  onCheckedChange={(enabled) =>
                    change(
                      enabled
                        ? `Enable ${grant.name} here?`
                        : `Disable ${grant.name} here?`,
                      enabled
                        ? "Execution uses this exact enabled workspace route and same-Node adapter."
                        : "Active runs are not stopped automatically.",
                      () => onRoute(ws, id, { enabled }),
                      !enabled,
                    )
                  }
                />
              </div>
            </div>
          );
        })}
        {(ws.available_adapters || []).length > 0 ? (
          <div className="execution-target-add">
            <select
              className="native-select"
              aria-label={`Add execution target for ${ws.name}`}
              id={`target-add-${ws.id}`}
              defaultValue=""
            >
              <option value="">Choose a same-Node adapter</option>
              {(ws.available_adapters || []).map((adapter) => (
                <option key={adapter.adapter_id} value={adapter.adapter_id}>
                  {adapter.name} · {runtimeName(adapter.runtime_type)}
                </option>
              ))}
            </select>
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                const select = document.getElementById(
                  `target-add-${ws.id}`,
                ) as HTMLSelectElement | null;
                if (!select?.value) {
                  onNotice("Choose an adapter owned by this Node first.");
                  return;
                }
                void onRoute(ws, select.value, { enabled: false });
                select.value = "";
              }}
            >
              <Plus size={14} /> Add target
            </Button>
          </div>
        ) : Object.keys(ws.routes || {}).length === 0 ? (
          <div className="execution-target-empty">
            <strong>No runtime adapters on this Node</strong>
            <p>
              Configure an adapter on the Nodes page before adding a target.
            </p>
            <Button
              variant="link"
              size="sm"
              className="inline-action"
              onClick={() => onNavigate("nodes")}
            >
              Configure Node adapters
            </Button>
          </div>
        ) : null}
      </div>
      <div className="workspace-footer">
        <span>
          <StateBadge value={ws.enabled ? "Bridge on" : "Bridge off"} />{" "}
          <span className="write-summary">
            {ws.write_scope === "none"
              ? "Read only"
              : ws.write_scope === "handoff"
                ? "Handoff writes"
                : "Workspace writes"}
          </span>
        </span>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => setSettingsOpen(!settingsOpen)}
          aria-expanded={settingsOpen}
        >
          <Settings2 size={15} /> Settings{" "}
          <ChevronDown className={settingsOpen ? "turned" : ""} size={15} />
        </Button>
      </div>
      {settingsOpen && (
        <div className="workspace-settings">
          <div className="settings-field">
            <Label htmlFor={`write-${ws.id}`}>File write access</Label>
            <select
              id={`write-${ws.id}`}
              className="native-select"
              value={writeScope}
              onChange={(e) =>
                setWriteScope(e.target.value as Workspace["write_scope"])
              }
            >
              <option value="none">Read only</option>
              <option value="handoff">Handoff files only</option>
              <option value="workspace">
                Permitted files across workspace
              </option>
            </select>
          </div>
          <div className="settings-field">
            <Label htmlFor={`excludes-${ws.id}`}>Extra exclusions</Label>
            <p>Built-in exclusions always apply. One pattern per line.</p>
            <Textarea
              id={`excludes-${ws.id}`}
              rows={3}
              value={excludes}
              onChange={(e) => setExcludes(e.target.value)}
            />
          </div>
          <Button
            className="settings-save"
            disabled={!settingsChanged}
            onClick={() =>
              change(
                "Save workspace settings?",
                writeScope !== ws.write_scope && writeScope === "workspace"
                  ? "Every chat with this bridge connection could change permitted source files in this workspace. Exclusions will be saved with this access level."
                  : "File write access and exclusions will be saved together.",
                async () => {
                  await api(`/api/workspaces/${ws.id}`, "POST", {
                    operation: "set_settings",
                    write_scope: writeScope,
                    excludes: exclusionPatterns,
                  });
                  onNotice("Workspace settings saved.");
                },
                writeScope !== ws.write_scope && writeScope === "workspace",
              )
            }
          >
            Save settings
          </Button>
        </div>
      )}
    </article>
  );
}

function thinkingEffortLabel(effort: string) {
  return (
    (
      {
        off: "Off",
        minimal: "Minimal",
        low: "Low",
        medium: "Medium",
        high: "High",
        xhigh: "Extra high",
        max: "Maximum",
        ultra: "Ultra",
      } as Record<string, string>
    )[effort] || effort
  );
}

function ModelDialog({
  adapterId,
  adapter,
  open,
  onClose,
  workspaces,
  policy,
  onSaved,
  onNotice,
}: {
  adapterId: string | null;
  adapter?: AdapterInfo;
  open: boolean;
  onClose: () => void;
  workspaces: Workspace[];
  policy?: ModelPolicy;
  onSaved: () => Promise<void>;
  onNotice: (text: string) => void;
}) {
  const eligible = workspaces.filter((w) => w.enabled);
  const [workspaceId, setWorkspaceId] = useState(eligible[0]?.id || "");
  const [models, setModels] = useState<Model[]>([]);
  const [enabled, setEnabled] = useState<string[]>(policy?.enabled || []);
  const [defaultModel, setDefaultModel] = useState(policy?.default || "");
  const [reasoningDefaults, setReasoningDefaults] = useState<
    Record<string, string>
  >(policy?.reasoning_defaults || {});
  const [filter, setFilter] = useState("");
  const [loading, setLoading] = useState(false);
  const load = useCallback(
    async (selected: string) => {
      if (!adapterId || !selected) return;
      setLoading(true);
      try {
        const data = await api<{ models: Model[]; policy?: ModelPolicy }>(
          `/api/adapters/${adapterId}/models?limit=100&workspace_id=${encodeURIComponent(selected)}`,
        );
        setModels(data.models || []);
        if (data.policy) {
          setEnabled(data.policy.enabled || []);
          setDefaultModel(data.policy.default || "");
          setReasoningDefaults(data.policy.reasoning_defaults || {});
        }
      } catch (error) {
        onNotice((error as Error).message);
      } finally {
        setLoading(false);
      }
    },
    [adapterId, onNotice],
  );
  useEffect(() => {
    if (open && workspaceId) void load(workspaceId);
  }, [open, workspaceId, load]);
  const visible = models.filter((m) =>
    `${m.selector} ${m.displayName || m.name || ""}`
      .toLowerCase()
      .includes(filter.toLowerCase()),
  );
  const allVisibleEnabled =
    visible.length > 0 && visible.every((m) => enabled.includes(m.selector));
  function toggleAllVisible() {
    if (!visible.length) return;
    setEnabled((old) =>
      allVisibleEnabled
        ? old.filter((x) => !visible.some((m) => m.selector === x))
        : [...new Set([...old, ...visible.map((m) => m.selector)])],
    );
    if (allVisibleEnabled && visible.some((m) => m.selector === defaultModel))
      setDefaultModel("");
  }
  async function save() {
    if (
      !adapterId ||
      !workspaceId ||
      !enabled.length ||
      !defaultModel ||
      !enabled.includes(defaultModel)
    ) {
      onNotice("Enable a model and choose its default.");
      return;
    }
    try {
      await api(`/api/adapters/${adapterId}/model-policy`, "POST", {
        workspace_id: workspaceId,
        enabled,
        default: defaultModel,
        reasoning_defaults: reasoningDefaults,
      });
      onNotice(`${adapter?.name || "Adapter"} model policy saved.`);
      onClose();
      await onSaved();
    } catch (error) {
      onNotice((error as Error).message);
    }
  }
  return (
    <Dialog open={open} onOpenChange={(value) => !value && onClose()}>
      <DialogContent className="model-dialog">
        <DialogHeader>
          <DialogTitle>{adapter?.name || "Adapter"} models</DialogTitle>
          <DialogDescription>
            Enable models, choose the default model, and set an optional
            thinking level for each model.
          </DialogDescription>
        </DialogHeader>
        {!eligible.length ? (
          <Empty title="Enable a workspace first">
            Model discovery needs an enabled workspace.
          </Empty>
        ) : (
          <>
            <div className="form-field">
              <Label htmlFor="model-workspace">Discovery workspace</Label>
              <select
                id="model-workspace"
                className="native-select"
                value={workspaceId}
                onChange={(e) => setWorkspaceId(e.target.value)}
              >
                {eligible.map((w) => (
                  <option key={w.id} value={w.id}>
                    {w.name}
                  </option>
                ))}
              </select>
            </div>
            <div className="model-search">
              <Input
                aria-label="Filter models"
                placeholder="Filter models"
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
              />
              <Button
                variant="outline"
                onClick={toggleAllVisible}
                disabled={loading || !visible.length}
              >
                {allVisibleEnabled ? "Deselect all" : "Select all"}
              </Button>
              <Button
                variant="outline"
                onClick={() => void load(workspaceId)}
                disabled={loading}
              >
                <RefreshCw size={15} /> Refresh
              </Button>
            </div>
            <div className="model-list">
              {loading ? (
                <Skeleton className="h-20" />
              ) : visible.length ? (
                visible.map((m) => {
                  const options = m.reasoningOptions || [];
                  const selectedEffort = reasoningDefaults[m.selector] || "";
                  const nativeDefault = m.defaultReasoningEffort
                    ? `Use adapter default (${thinkingEffortLabel(m.defaultReasoningEffort)})`
                    : "Use adapter default";
                  return (
                    <div
                      className={
                        adapter?.runtime_type === "codex" ||
                        adapter?.runtime_type === "claude"
                          ? "model-option model-option-codex"
                          : "model-option"
                      }
                      key={m.selector}
                    >
                      <label className="model-option-main">
                        <input
                          type="checkbox"
                          checked={enabled.includes(m.selector)}
                          onChange={(e) => {
                            setEnabled((old) =>
                              e.target.checked
                                ? [...old, m.selector]
                                : old.filter((x) => x !== m.selector),
                            );
                            if (
                              !e.target.checked &&
                              defaultModel === m.selector
                            )
                              setDefaultModel("");
                          }}
                        />
                        <span>
                          <strong>
                            {m.displayName || m.name || m.model || m.selector}
                          </strong>
                          <small>{m.selector}</small>
                        </span>
                      </label>
                      {options.length > 0 && (
                        <select
                          className="native-select model-thinking-select"
                          aria-label={`Default thinking level for ${m.displayName || m.selector}`}
                          value={selectedEffort}
                          onChange={(e) => {
                            const value = e.target.value;
                            setReasoningDefaults((old) => {
                              const next = { ...old };
                              if (value) next[m.selector] = value;
                              else delete next[m.selector];
                              return next;
                            });
                          }}
                        >
                          <option value="">{nativeDefault}</option>
                          {selectedEffort &&
                            !options.includes(selectedEffort) && (
                              <option value={selectedEffort}>
                                Unavailable (
                                {thinkingEffortLabel(selectedEffort)})
                              </option>
                            )}
                          {options.map((effort) => (
                            <option key={effort} value={effort}>
                              {thinkingEffortLabel(effort)}
                            </option>
                          ))}
                        </select>
                      )}
                    </div>
                  );
                })
              ) : (
                <p className="muted-note">
                  {models.length ? "No models match." : "No models discovered."}
                </p>
              )}
            </div>
            <div className="form-field">
              <Label htmlFor="default-model">Default model</Label>
              <select
                id="default-model"
                className="native-select"
                value={defaultModel}
                onChange={(e) => setDefaultModel(e.target.value)}
              >
                <option value="">Choose an enabled model</option>
                {models
                  .filter((m) => enabled.includes(m.selector))
                  .map((m) => (
                    <option key={m.selector} value={m.selector}>
                      {m.displayName || m.selector}
                    </option>
                  ))}
              </select>
            </div>
          </>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={() => void save()} disabled={!eligible.length}>
            Save model policy
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default function App() {
  const [auth, setAuth] = useState<"checking" | "login" | "password" | "ready">(
    "checking",
  );
  const [loginError, setLoginError] = useState("");
  const [changingPassword, setChangingPassword] = useState(false);
  const [authBusy, setAuthBusy] = useState(false);
  const [theme, setTheme] = useState(initialTheme);
  const [section, setSection] = useState<Section>(initialSection);
  const [mobileNav, setMobileNav] = useState(false);
  const [status, setStatus] = useState<Status | null>(null);
  const [nodes, setNodes] = useState<NodeInfo[]>([]);
  const [diagnostics, setDiagnostics] = useState<DiagnosticReport | null>(null);
  const [diagnosticsError, setDiagnosticsError] = useState<string | null>(null);
  const [versions, setVersions] = useState<VersionStatus | null>(null);
  const [versionsError, setVersionsError] = useState<string | null>(null);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [events, setEvents] = useState<Event[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [runsNext, setRunsNext] = useState<number | null>(null);
  const [selectedWorkspace, setSelectedWorkspace] = useState<string | null>(
    null,
  );
  const [selectedRouteRuntime, setSelectedRouteRuntime] = useState<
    string | null
  >(null);
  const [handoffs, setHandoffs] = useState<Handoff[]>([]);
  const [workspaceRuns, setWorkspaceRuns] = useState<Run[]>([]);
  const [workspaceQuery, setWorkspaceQuery] = useState("");
  const [addOpen, setAddOpen] = useState(false);
  const [adapterDialog, setAdapterDialog] = useState<{
    mode: "create" | "edit";
    adapter?: AdapterInfo;
    nodeId?: string;
  } | null>(null);
  const [adapterTest, setAdapterTest] = useState("");
  const [nodeDialog, setNodeDialog] = useState<{
    mode: "create" | "edit";
    node?: NodeInfo;
  } | null>(null);
  const [nodeTest, setNodeTest] = useState("");
  const [profileFor, setProfileFor] = useState<{
    ws: Workspace;
    adapterId: string;
  } | null>(null);
  const [modelAdapterId, setModelAdapterId] = useState<string | null>(null);
  const [runInspect, setRunInspect] = useState<Run | null>(null);
  const [textDetail, setTextDetail] = useState<TextDetail | null>(null);
  const [confirm, setConfirm] = useState<ConfirmState | null>(null);
  const [notice, setNotice] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const [startingHandoff, setStartingHandoff] = useState<string | null>(null);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const refreshingRef = useRef(false);
  const startRequestIds = useRef(new Map<string, string>());
  const notify = useCallback((text: string) => {
    setNotice(text);
    window.setTimeout(() => setNotice(""), 6500);
  }, []);
  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
    try {
      localStorage.setItem("wb-theme", theme);
    } catch {
      /* storage unavailable */
    }
  }, [theme]);
  const navigate = useCallback((next: Section) => {
    setSection(next);
    setMobileNav(false);
    if (location.hash !== `#${next}`)
      history.replaceState(null, "", `#${next}`);
    window.scrollTo(0, 0);
  }, []);
  useEffect(() => {
    const change = () => setSection(initialSection());
    window.addEventListener("hashchange", change);
    return () => window.removeEventListener("hashchange", change);
  }, []);
  const loadRuns = useCallback(async (offset = 0) => {
    const data = await api<{ runs: Run[]; next_offset: number | null }>(
      `/api/runs?offset=${offset}&limit=${pageSize}`,
    );
    setRuns((old) => (offset ? [...old, ...data.runs] : data.runs));
    setRunsNext(data.next_offset);
    return data;
  }, []);
  const refresh = useCallback(
    async (options?: { silent?: boolean }) => {
      if (refreshingRef.current) return;
      refreshingRef.current = true;
      setRefreshing(true);
      try {
        const diagnosticsResult = api<unknown>("/api/diagnostics").then(
          (value) => {
            if (isDiagnosticReport(value)) {
              setDiagnostics(value);
              setDiagnosticsError(null);
            } else {
              setDiagnosticsError("Diagnostics returned an invalid report.");
            }
          },
          (error: unknown) => {
            setDiagnosticsError(
              error instanceof Error
                ? error.message
                : "Diagnostics could not be refreshed.",
            );
          },
        );
        const versionsResult = api<VersionStatus>("/api/system/versions").then(
          (value) => {
            setVersions(value);
            setVersionsError(null);
          },
          (error: unknown) => {
            setVersionsError(
              error instanceof Error
                ? error.message
                : "Versions status could not be refreshed.",
            );
          },
        );
        const [workspaceData, historyData, currentStatus] = await Promise.all([
          api<{ workspaces: Workspace[] }>("/api/workspaces"),
          api<{ events: Event[] }>("/api/events"),
          api<Status>("/api/status"),
          diagnosticsResult,
          versionsResult,
        ]);
        setWorkspaces(workspaceData.workspaces || []);
        setEvents(historyData.events || []);
        setStatus(currentStatus);
        setNodes(currentStatus.nodes || []);
        try {
          await loadRuns();
        } catch (error) {
          if (!options?.silent) notify((error as Error).message);
        }
        setLastUpdated(Date.now());
      } catch (error) {
        if (!options?.silent) throw error;
      } finally {
        refreshingRef.current = false;
        setRefreshing(false);
      }
    },
    [loadRuns, notify],
  );
  const loadWorkspaceDetails = useCallback(async (workspaceId: string) => {
    const [a, b] = await Promise.all([
      api<{ handoffs: Handoff[] }>(`/api/workspaces/${workspaceId}/jobs`),
      api<{ runs: Run[] }>(`/api/workspaces/${workspaceId}/runs`),
    ]);
    setHandoffs(a.handoffs || []);
    setWorkspaceRuns(b.runs || []);
  }, []);
  useEffect(() => {
    void (async () => {
      try {
        const account = await api<{ must_change_password: boolean }>(
          "/api/account",
        );
        if (account.must_change_password) {
          setAuth("password");
          return;
        }
        await refresh();
        setAuth("ready");
      } catch {
        setAuth("login");
      }
    })();
  }, [refresh]);
  useEffect(() => {
    if (auth !== "ready" || !autoRefresh) return;
    const tick = () => {
      if (document.hidden || refreshingRef.current) return;
      void (async () => {
        try {
          await refresh({ silent: true });
          if (section === "handoffs" && selectedWorkspace) {
            try {
              await loadWorkspaceDetails(selectedWorkspace);
            } catch {
              /* keep stale handoffs until next tick */
            }
          }
        } catch {
          /* keep stale page data until next tick */
        }
      })();
    };
    const timer = window.setInterval(tick, autoRefreshMs);
    const onVisible = () => {
      if (!document.hidden) tick();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [
    auth,
    autoRefresh,
    refresh,
    section,
    selectedWorkspace,
    loadWorkspaceDetails,
  ]);
  async function login(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setLoginError("");
    const form = event.currentTarget;
    const data = new FormData(form);
    setAuthBusy(true);
    try {
      const account = await api<{ must_change_password: boolean }>(
        "/api/login",
        "POST",
        {
          username: data.get("username"),
          password: data.get("password"),
        },
      );
      form.reset();
      if (account.must_change_password) {
        setAuth("password");
      } else {
        await refresh();
        setAuth("ready");
      }
    } catch (error) {
      setLoginError((error as Error).message);
    } finally {
      setAuthBusy(false);
    }
  }
  async function changePassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setLoginError("");
    const form = event.currentTarget;
    const data = new FormData(form);
    if (data.get("new_password") !== data.get("confirm_password")) {
      setLoginError("New passwords do not match.");
      return;
    }
    setAuthBusy(true);
    try {
      await api("/api/account/password", "POST", {
        current_password: data.get("current_password"),
        new_password: data.get("new_password"),
      });
      form.reset();
      await refresh();
      setChangingPassword(false);
      setAuth("ready");
      setLoginError("");
    } catch (error) {
      setLoginError((error as Error).message);
    } finally {
      setAuthBusy(false);
    }
  }
  async function logout() {
    try {
      await fetch("/api/logout", {
        method: "POST",
        credentials: "same-origin",
      });
    } catch {
      /* local cleanup still applies */
    }
    setAuth("login");
    setChangingPassword(false);
    setLoginError("");
    setStatus(null);
    setDiagnostics(null);
    setDiagnosticsError(null);
    setWorkspaces([]);
    setEvents([]);
    setRuns([]);
    setHandoffs([]);
    setRunInspect(null);
    setTextDetail(null);
    setUsageLimits({});
  }
  async function manage(ws: Workspace, operation: string, extra: Json = {}) {
    try {
      const result = await api<{ token?: string; note?: string }>(
        `/api/workspaces/${ws.id}`,
        "POST",
        { operation, ...extra },
      );
      if (result.token)
        setTextDetail({
          title: "Bridge credential — shown once",
          content: result.token,
        });
      notify(result.note || "Workspace updated.");
      await refresh();
    } catch (error) {
      notify((error as Error).message);
    }
  }
  async function changeRoute(ws: Workspace, adapterId: string, body: Json) {
    try {
      if (body.clear_default) {
        await api(`/api/workspaces/${ws.id}/routes/default`, "DELETE");
      } else if (body.is_default) {
        await api(`/api/workspaces/${ws.id}/routes/default`, "POST", {
          adapter_id: adapterId,
        });
      } else {
        await api(`/api/workspaces/${ws.id}/routes/${adapterId}`, "POST", body);
      }
      notify("Execution target updated.");
      await refresh();
    } catch (error) {
      notify((error as Error).message);
    }
  }
  async function saveAdapter(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!adapterDialog) return;
    const data = new FormData(event.currentTarget);
    const body =
      adapterDialog.mode === "create"
        ? {
            name: data.get("name"),
            node_id: data.get("node_id") || adapterDialog.nodeId,
            runtime_type: data.get("runtime_type"),
            base_url: data.get("base_url"),
            token: data.get("token"),
            enabled: data.get("enabled") === "on",
          }
        : {
            name: data.get("name"),
            base_url: data.get("base_url"),
            token: data.get("token"),
            enabled: data.get("enabled") === "on",
          };
    try {
      if (adapterDialog.mode === "create")
        await api("/api/adapters", "POST", body);
      else
        await api(`/api/adapters/${adapterDialog.adapter?.id}`, "PATCH", body);
      setAdapterDialog(null);
      setAdapterTest("");
      notify(
        adapterDialog.mode === "create" ? "Adapter added." : "Adapter updated.",
      );
      await refresh();
    } catch (error) {
      notify((error as Error).message);
    }
  }
  async function saveNode(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!nodeDialog) return;
    const data = new FormData(event.currentTarget);
    const body = {
      name: data.get("name"),
      base_url: data.get("base_url"),
      token: data.get("token"),
      enabled: data.get("enabled") === "on",
    };
    try {
      await api(
        nodeDialog.mode === "create"
          ? "/api/nodes"
          : `/api/nodes/${nodeDialog.node?.id}`,
        nodeDialog.mode === "create" ? "POST" : "PATCH",
        body,
      );
      setNodeDialog(null);
      setNodeTest("");
      notify(nodeDialog.mode === "create" ? "Node added." : "Node updated.");
      await refresh();
    } catch (error) {
      notify((error as Error).message);
    }
  }
  async function testNode(form?: HTMLFormElement) {
    if (!nodeDialog || !form) return;
    const data = new FormData(form);
    try {
      const result = await api<{
        success: boolean;
        message?: string;
        code?: string;
      }>("/api/nodes/test", "POST", {
        node_id: nodeDialog.node?.id,
        name: data.get("name"),
        base_url: data.get("base_url"),
        token: data.get("token"),
        enabled: data.get("enabled") === "on",
      });
      setNodeTest(
        result.success
          ? "Connected: Node Protocol is ready."
          : `Connection failed: ${result.message || result.code || "Unavailable"}`,
      );
    } catch (error) {
      setNodeTest(`Connection failed: ${(error as Error).message}`);
    }
  }
  async function testAdapter(form?: HTMLFormElement) {
    if (!adapterDialog) return;
    try {
      let result: {
        success: boolean;
        message?: string;
        code?: string;
        native_runtime?: string;
        native_instance?: string;
        adapter_version?: string;
        native_version?: string;
      };
      const data = new FormData(form);
      result = await api("/api/adapters/test", "POST", {
        node_id: data.get("node_id") || adapterDialog.nodeId,
        name: data.get("name"),
        runtime_type:
          adapterDialog.mode === "edit"
            ? adapterDialog.adapter?.runtime_type
            : data.get("runtime_type"),
        base_url: data.get("base_url"),
        token: data.get("token"),
        enabled: data.get("enabled") === "on",
        ...(adapterDialog.mode === "edit"
          ? { adapter_id: adapterDialog.adapter?.id }
          : {}),
      });
      setAdapterTest(
        result.success
          ? `Connected: ${result.native_runtime} · ${result.native_version || result.adapter_version || "version unavailable"}`
          : `Connection failed: ${result.message || result.code || "Unavailable"}`,
      );
    } catch (error) {
      setAdapterTest(`Connection failed: ${(error as Error).message}`);
    }
  }
  function ask(state: ConfirmState) {
    setConfirm(state);
  }
  async function executeConfirmed() {
    const pending = confirm;
    setConfirm(null);
    if (!pending) return;
    try {
      await pending.action();
      await refresh();
    } catch (error) {
      notify((error as Error).message);
    }
  }
  async function openHandoffs(ws: Workspace) {
    setSelectedWorkspace(ws.id);
    setSelectedRouteRuntime(null);
    navigate("handoffs");
    try {
      await loadWorkspaceDetails(ws.id);
    } catch (error) {
      notify((error as Error).message);
    }
  }
  async function startPreparedHandoff(handoff: Handoff, route: RunnableRoute) {
    if (startingHandoff) return;
    const key = `${selectedWorkspace}:${handoff.id}:${route.adapter_id}`;
    let requestId = startRequestIds.current.get(key);
    if (!requestId) {
      requestId = crypto.randomUUID();
      startRequestIds.current.set(key, requestId);
    }
    setStartingHandoff(key);
    try {
      const response = await fetch(
        `/api/workspaces/${selectedWorkspace}/jobs/${handoff.id}/runs`,
        {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            adapter_id: route.adapter_id,
            request_id: requestId,
          }),
        },
      );
      const result = (await response.json().catch(() => ({}))) as Run & {
        error?: string;
      };
      if (!response.ok) {
        if (response.status >= 400 && response.status < 500)
          startRequestIds.current.delete(key);
        notify(
          result.error ||
            (response.status >= 500
              ? "The start outcome is uncertain. Retry to reuse the same request ID."
              : `Could not start the prepared handoff (HTTP ${response.status}).`),
        );
        return;
      }
      if (!result.run_id) {
        notify(
          "The start outcome is uncertain. Retry to reuse the same request ID.",
        );
        return;
      }
      startRequestIds.current.delete(key);
      notify(
        `Started ${route.adapter_name} run${result.model ? ` with ${result.model}` : ""}.`,
      );
      await Promise.allSettled([
        refresh({ silent: true }),
        loadWorkspaceDetails(selectedWorkspace || ""),
      ]);
      navigate("runs");
    } catch {
      notify(
        "The start outcome is uncertain. Retry to reuse the same request ID.",
      );
    } finally {
      setStartingHandoff(null);
    }
  }
  function openProfile(ws: Workspace, adapterId: string) {
    setProfileFor({ ws, adapterId });
  }
  async function addWorkspace(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    try {
      const result = await api<{ note?: string }>("/api/workspaces", "POST", {
        name: data.get("name"),
        root: data.get("root"),
        node_id: data.get("node_id"),
        excludes: String(data.get("excludes") || "")
          .split("\n")
          .map((x) => x.trim())
          .filter(Boolean),
      });
      notify(result.note || "Workspace added.");
      setAddOpen(false);
      form.reset();
      await refresh();
    } catch (error) {
      notify((error as Error).message);
    }
  }
  async function bridgeChange(operation: string) {
    try {
      const result = await api<{ token?: string }>("/api/bridge", "POST", {
        operation,
      });
      if (result.token)
        setTextDetail({
          title: "Bridge credential — shown once",
          content: `Save this token in the private tunnel environment. The previous token is revoked.\n\n${result.token}`,
        });
      notify(
        operation === "rotate_token"
          ? "Bridge credential created."
          : operation === "enable"
            ? "Bridge enabled."
            : "Bridge paused.",
      );
      await refresh();
    } catch (error) {
      notify((error as Error).message);
    }
  }
  const selected = workspaces.find((w) => w.id === selectedWorkspace) || null;
  const filtered = workspaces.filter((w) =>
    `${w.name} ${w.root}`.toLowerCase().includes(workspaceQuery.toLowerCase()),
  );
  const adapters = status?.adapters?.adapters || [];
  const policies = useMemo(
    () =>
      Object.fromEntries(
        adapters.map((adapter) => [adapter.id, adapter.model_policy || {}]),
      ),
    [adapters],
  );
  const [usageLimits, setUsageLimits] = useState<
    Record<string, UsageLimits | null>
  >({});
  const quotaEligible = useCallback(
    (info: AdapterInfo) =>
      Boolean(
        info.enabled &&
        info.healthy &&
        (info.runtime_type === "codex" || info.runtime_type === "claude") &&
        info.features?.usageLimits === 1,
      ),
    [],
  );
  // A stable string key for the current set of quota-capable adapters keeps
  // the fetch effect bounded. The effect owns its immediate read, its own
  // polling timer, and its cleanup; global status refreshes do not cancel
  // or discard an in-flight quota response.
  const quotaTargetsKey =
    section === "adapters"
      ? adapters
          .filter(quotaEligible)
          .map((info) => info.id)
          .sort()
          .join(",")
      : "";
  useEffect(() => {
    // Quota is fetched only while the Adapters section is active/visible and
    // only for enabled, healthy adapters advertising usageLimits. Failures
    // are recorded per adapter and leave the rest of the page working.
    if (!quotaTargetsKey) return;
    const ids = quotaTargetsKey.split(",").filter(Boolean);
    if (!ids.length) return;
    let cancelled = false;
    let requestInFlight = false;
    let timer: number | undefined;
    const loadQuota = async () => {
      if (cancelled || requestInFlight || document.hidden) return;
      requestInFlight = true;
      try {
        const results = await Promise.all(
          ids.map(async (id) => {
            try {
              const limits = await api<UsageLimits>(
                `/api/adapters/${id}/usage-limits`,
              );
              return [id, limits] as const;
            } catch {
              return [id, null] as const;
            }
          }),
        );
        if (!cancelled)
          setUsageLimits((old) => ({ ...old, ...Object.fromEntries(results) }));
      } finally {
        requestInFlight = false;
      }
    };
    void loadQuota();
    timer = window.setInterval(() => {
      void loadQuota();
    }, quotaRefreshMs);
    const handleQuotaVisibility = () => {
      if (!document.hidden) void loadQuota();
    };
    document.addEventListener("visibilitychange", handleQuotaVisibility);
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearInterval(timer);
      document.removeEventListener("visibilitychange", handleQuotaVisibility);
    };
  }, [quotaTargetsKey, quotaEligible]);
  const attentionRuns = runs.filter(
    (r) => r.active_state === "waiting_interaction",
  );
  const diagnosticsUnavailable = !diagnostics || Boolean(diagnosticsError);
  const readyRoutes = useMemo(
    () =>
      diagnosticsUnavailable
        ? []
        : (diagnostics?.runnable_routes || []).filter((route) => route.ready),
    [diagnostics, diagnosticsUnavailable],
  );
  const selectedRoutes = (diagnostics?.runnable_routes || []).filter(
    (route) => route.workspace_id === selectedWorkspace,
  );
  const selectedWorkspaceRecord = workspaces.find(
    (workspace) => workspace.id === selectedWorkspace,
  );
  const configuredDefaultRoute = selectedRoutes.find(
    (route) =>
      route.is_default ||
      selectedWorkspaceRecord?.routes?.[route.adapter_id]?.is_default,
  );
  const readySelectedRoutes = selectedRoutes.filter((route) => route.ready);
  const effectiveSelectedRouteRuntime = selectedRoutes.some(
    (route) => route.adapter_id === selectedRouteRuntime,
  )
    ? selectedRouteRuntime
    : (
        configuredDefaultRoute ||
        (readySelectedRoutes.length === 1 ? readySelectedRoutes[0] : null)
      )?.adapter_id || null;
  const selectedRoute = selectedRoutes.find(
    (route) => route.adapter_id === effectiveSelectedRouteRuntime,
  );
  const setup = useMemo(() => {
    const passed = (code: string) =>
      !diagnosticsUnavailable &&
      Boolean(
        diagnostics?.checks.some(
          (check) => check.code === code && check.status === "pass",
        ),
      );
    return [
      {
        title: "Enable a workspace",
        done: passed("workspace.enabled"),
        target: "workspaces" as Section,
        text: "Choose a project mapping and enable Bridge access.",
      },
      {
        title: "Enable the Bridge gateway",
        done: passed("core.gateway_enabled"),
        target: "adapters" as Section,
        text: "Configure and enable the shared MCP gateway.",
      },
      {
        title: "Prepare a runnable route",
        done: readyRoutes.length > 0,
        target: "workspaces" as Section,
        text: "Review exact workspace and adapter route readiness.",
      },
    ];
  }, [diagnostics, diagnosticsUnavailable, readyRoutes]);
  const page = sections.find((s) => s.id === section)!;
  const ready = auth === "ready" && !changingPassword;
  useEffect(() => {
    if (!lastUpdated) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [lastUpdated]);
  const focusWorkspaceSearch = useCallback(() => {
    navigate("workspaces");
    window.setTimeout(
      () => document.getElementById("workspace-search")?.focus(),
      0,
    );
  }, [navigate]);
  useEffect(() => {
    if (!ready) return;
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPaletteOpen((open) => !open);
        return;
      }
      if (
        event.metaKey ||
        event.ctrlKey ||
        event.altKey ||
        event.defaultPrevented ||
        isTypingTarget(event.target)
      )
        return;
      const index = Number(event.key) - 1;
      if (Number.isInteger(index) && index >= 0 && index < sections.length) {
        event.preventDefault();
        navigate(sections[index].id);
      } else if (event.key === "/") {
        event.preventDefault();
        focusWorkspaceSearch();
      } else if (event.key === ":" || event.key === "?") {
        event.preventDefault();
        setPaletteOpen(true);
      } else if (event.key === "r") {
        event.preventDefault();
        void refresh();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [ready, navigate, refresh, focusWorkspaceSearch]);
  const commands = useMemo<PaletteCommand[]>(
    () => [
      ...sections.map((s, index) => ({
        id: `go-${s.id}`,
        group: "Go to",
        label: s.title,
        keys: String(index + 1),
        run: () => navigate(s.id),
      })),
      {
        id: "search-workspaces",
        group: "Actions",
        label: "Search workspaces",
        keys: "/",
        run: focusWorkspaceSearch,
      },
      {
        id: "add-workspace",
        group: "Actions",
        label: "Add a workspace",
        run: () => {
          navigate("workspaces");
          setAddOpen(true);
        },
      },
      {
        id: "refresh",
        group: "Actions",
        label: "Refresh data now",
        keys: "r",
        run: () => void refresh(),
      },
      {
        id: "auto-refresh",
        group: "Actions",
        label: autoRefresh
          ? "Pause auto-refresh"
          : "Resume auto-refresh (every 30s)",
        run: () => setAutoRefresh(!autoRefresh),
      },
      {
        id: "theme",
        group: "Console",
        label: `Switch to ${theme === "dark" ? "light" : "dark"} theme`,
        run: () => setTheme(theme === "dark" ? "light" : "dark"),
      },
      {
        id: "password",
        group: "Console",
        label: "Change password",
        run: () => {
          setLoginError("");
          setChangingPassword(true);
        },
      },
      {
        id: "lock",
        group: "Console",
        label: "Lock console",
        run: () => void logout(),
      },
    ],
    [navigate, focusWorkspaceSearch, refresh, autoRefresh, theme],
  );
  if (auth === "checking")
    return (
      <div className="loading-shell">
        <div className="bridge-mark">
          <SquareTerminal size={26} />
        </div>
        <p className="boot-line" aria-hidden="true">
          checking session
          <span className="cursor" />
        </p>
        <Skeleton className="h-7 w-52" />
        <Skeleton className="h-4 w-72" />
        <span className="sr-only">Checking session</span>
      </div>
    );
  if (auth === "password" || changingPassword)
    return (
      <div className="login-shell">
        <main className="login-main">
          <div className="login-intro">
            <span className="section-kicker">Admin account</span>
            <h1>Change your password</h1>
            <p>
              {auth === "password"
                ? "Choose a new password before using the Manager."
                : "Update the password for your admin account."}
            </p>
          </div>
          <div className="login-panel">
            <h2>
              {auth === "password"
                ? "Replace the temporary password"
                : "New password"}
            </h2>
            <form onSubmit={changePassword}>
              <Label htmlFor="current-password">Current password</Label>
              <Input
                id="current-password"
                name="current_password"
                type="password"
                autoComplete="current-password"
                maxLength={256}
                required
              />
              <Label htmlFor="new-password">New password</Label>
              <Input
                id="new-password"
                name="new_password"
                type="password"
                autoComplete="new-password"
                minLength={8}
                maxLength={256}
                required
              />
              <Label htmlFor="confirm-password">Confirm new password</Label>
              <Input
                id="confirm-password"
                name="confirm_password"
                type="password"
                autoComplete="new-password"
                minLength={8}
                maxLength={256}
                required
              />
              <small>
                Use 8 to 256 characters. Other sessions will be signed out.
              </small>
              <Button type="submit" size="lg" disabled={authBusy}>
                {authBusy ? "Saving…" : "Save password"}
              </Button>
              {changingPassword ? (
                <Button
                  type="button"
                  variant="ghost"
                  onClick={() => {
                    setChangingPassword(false);
                    setLoginError("");
                  }}
                >
                  Cancel
                </Button>
              ) : (
                <Button
                  type="button"
                  variant="ghost"
                  onClick={() => void logout()}
                >
                  Sign out
                </Button>
              )}
            </form>
            {loginError && (
              <p className="form-error" role="alert">
                {loginError}
              </p>
            )}
          </div>
        </main>
      </div>
    );
  if (auth === "login")
    return (
      <div className="login-shell">
        <div className="login-top">
          <Brand />
          <Button
            variant="ghost"
            size="icon"
            aria-label="Toggle theme"
            onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
          >
            {theme === "dark" ? <Sun /> : <Moon />}
          </Button>
        </div>
        <main className="login-main">
          <div className="login-intro">
            <pre className="boot-log" aria-hidden="true">
              <span>
                <b>$</b> workspace-bridge manager
              </span>
              <span>
                <i>[ ok ]</i> loopback listener bound
              </span>
              <span>
                <i>[ ok ]</i> node registry loaded
              </span>
              <span>
                <i>[ ok ]</i> audit log attached
              </span>
              <span>
                <em>[wait]</em> operator authentication
              </span>
            </pre>
            <h1>
              Your projects.
              <br />
              Your agents.
              <br />
              One clear view.
            </h1>
            <p>
              Workspace Bridge keeps access, handoffs, and running agents
              visible in one local console.
            </p>
          </div>
          <div className="login-panel">
            <div className="window-bar" aria-hidden="true">
              <i />
              <i />
              <i />
              <span>login — tty1</span>
            </div>
            <span className="section-kicker">Local manager</span>
            <h2>Open the console</h2>
            <p>Sign in with your admin account to manage this Bridge.</p>
            <form onSubmit={login}>
              <Label htmlFor="admin-username">Username</Label>
              <Input
                id="admin-username"
                name="username"
                autoComplete="username"
                required
              />
              <Label htmlFor="admin-password">Password</Label>
              <Input
                id="admin-password"
                name="password"
                type="password"
                autoComplete="current-password"
                maxLength={256}
                required
              />
              <Button type="submit" size="lg" disabled={authBusy}>
                Sign in <ArrowRight size={16} />
              </Button>
            </form>
            {loginError && (
              <p className="form-error" role="alert">
                {loginError}
              </p>
            )}
            <small>
              Forgot your password? Run workspace-bridge reset-admin-password on
              the Bridge host.
            </small>
          </div>
        </main>
        <footer className="login-foot">loopback only · never tunnelled</footer>
      </div>
    );
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        Skip to content
      </a>
      <aside className="sidebar">
        <Brand />
        <div className="sidebar-label">OPERATIONS</div>
        <nav aria-label="Main navigation">
          {sections.map((s, index) => (
            <button
              type="button"
              key={s.id}
              className={`nav-item ${section === s.id ? "current" : ""}`}
              aria-current={section === s.id ? "page" : undefined}
              aria-keyshortcuts={String(index + 1)}
              onClick={() => navigate(s.id)}
            >
              <s.icon size={16} />
              <span>{s.title}</span>
              {s.id === "runs" && attentionRuns.length > 0 && (
                <b>{attentionRuns.length}</b>
              )}
              <kbd aria-hidden="true">{index + 1}</kbd>
            </button>
          ))}
        </nav>
        <button
          type="button"
          className="palette-trigger"
          aria-keyshortcuts="Control+K Meta+K"
          onClick={() => setPaletteOpen(true)}
        >
          <SquareTerminal size={15} />
          <span>Commands</span>
          <kbd aria-hidden="true">ctrl k</kbd>
        </button>
        <div className="sidebar-bottom">
          <div className="local-status">
            <span className="live-dot" />
            Local manager <strong>Connected</strong>
          </div>
          <Button
            variant="ghost"
            className="lock-button"
            onClick={() => void logout()}
          >
            <LockKeyhole size={15} /> Lock console
          </Button>
        </div>
      </aside>
      <div className="workspace-shell">
        <header className="topbar">
          <div className="topbar-left">
            <Button
              variant="ghost"
              size="icon"
              className="mobile-menu"
              aria-label="Open navigation"
              onClick={() => setMobileNav(true)}
            >
              <Menu />
            </Button>
            <span className="breadcrumb">admin@bridge</span>
            <span className="breadcrumb-sep">:~/</span>
            <strong>{page.id}</strong>
          </div>
          <div className="topbar-right">
            <span className="topbar-health">
              <span className="live-dot" />
              {!status?.bridge.configured
                ? "Bridge gateway not configured"
                : status.bridge.enabled
                  ? "Bridge gateway enabled"
                  : "Bridge gateway paused"}
            </span>
            <Button
              variant="ghost"
              size="icon"
              aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} mode`}
              onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
            >
              {theme === "dark" ? <Sun size={18} /> : <Moon size={18} />}
            </Button>
            <Button
              variant="ghost"
              onClick={() => {
                setLoginError("");
                setChangingPassword(true);
              }}
            >
              Change password
            </Button>
            <Button
              variant="ghost"
              size="icon"
              aria-label="Lock console"
              className="mobile-lock"
              onClick={() => void logout()}
            >
              <LockKeyhole size={18} />
            </Button>
          </div>
        </header>
        <main id="main-content" className="main-content">
          <div className="page-title">
            <div>
              <span className="page-prompt" aria-hidden="true">
                <b>$</b> wb {page.id}
              </span>
              <h1>
                {page.title}
                <span className="cursor" aria-hidden="true" />
              </h1>
              <p>{page.description}</p>
            </div>
            <div className="page-title-actions">
              <label className="auto-refresh-toggle">
                <Switch
                  aria-label="Auto-refresh data every 30 seconds"
                  checked={autoRefresh}
                  onCheckedChange={setAutoRefresh}
                />
                <span>Auto</span>
              </label>
              {lastUpdated && (
                <span
                  className="last-updated"
                  title={new Date(lastUpdated).toLocaleString()}
                >
                  Updated {relativeTime(lastUpdated, now)}
                </span>
              )}
              <Button
                variant="outline"
                size="sm"
                onClick={() => void refresh()}
                disabled={refreshing}
              >
                <RefreshCw size={15} className={refreshing ? "spinning" : ""} />{" "}
                Refresh
              </Button>
            </div>
          </div>
          {section === "overview" && (
            <div className="overview-page">
              <section className="surface-panel diagnostic-health">
                <SectionHeading
                  title="System health"
                  description="Diagnostic health and observations; this does not determine route readiness."
                  action={
                    diagnostics && (
                      <StateBadge
                        value={
                          diagnosticsUnavailable
                            ? "Stale / unavailable"
                            : `Health ${diagnostics.overall.status}`
                        }
                      />
                    )
                  }
                />
                {diagnosticsUnavailable ? (
                  <div className="diagnostic-unavailable" role="status">
                    <strong>
                      Current system observations are unavailable.
                    </strong>
                    {diagnostics?.generated_at && (
                      <p>
                        Last successful diagnostics:{" "}
                        {dateTime(diagnostics.generated_at)}. This snapshot is
                        stale and cannot authorize a start.
                      </p>
                    )}
                  </div>
                ) : (
                  <>
                    <p className="diagnostic-health-summary">
                      {diagnostics?.overall.summary}
                    </p>
                    <div
                      className="diagnostic-counts"
                      aria-label="Diagnostic check counts"
                    >
                      {(
                        [
                          "pass",
                          "warning",
                          "unknown",
                          "action_required",
                          "failed",
                        ] as const
                      ).map((state) => (
                        <span key={state}>
                          <strong>
                            {diagnostics?.overall.counts[state] || 0}
                          </strong>
                          {state.replaceAll("_", " ")}
                        </span>
                      ))}
                    </div>
                  </>
                )}
                {(() => {
                  if (!status) return null;
                  const compiled = compiledManagerReleaseRaw();
                  const served = status?.manager_release ?? null;
                  const identity = describeManagerIdentity(compiled, served);
                  if (identity === "mismatch") {
                    return (
                      <div className="diagnostic-warnings" role="alert">
                        <strong>
                          Manager build mismatch / refresh or rebuild required
                        </strong>
                        <div>
                          <span>
                            This cached Manager build differs from the
                            Bridge-served build. Refresh the page or rebuild the
                            Manager; no data was changed.
                          </span>
                        </div>
                      </div>
                    );
                  }
                  if (identity === "unavailable") {
                    return (
                      <div className="diagnostic-unavailable" role="status">
                        <strong>Manager identity unavailable</strong>
                        <p>
                          The Bridge did not report a compiled Manager build.
                          This is not a mismatch; refresh or rebuild the
                          Manager.
                        </p>
                      </div>
                    );
                  }
                  return null;
                })()}
                <div className="diagnostic-observation-list">
                  <p>
                    Bridge gateway:{" "}
                    {status?.bridge.configured
                      ? "configured"
                      : "not configured"}{" "}
                    · {status?.bridge.enabled ? "enabled" : "disabled"}
                  </p>
                  <p
                    title={status?.release?.build_id || "Bridge build unknown"}
                  >
                    Bridge release{" "}
                    {status?.release?.product_version ||
                      status?.version ||
                      "unknown"}{" "}
                    · {shortBuildId(status?.release?.build_id)}
                    {versions?.target?.product_version && (
                      <>
                        {" "}
                        · target {versions.target.product_version}{" "}
                        <Button
                          variant="link"
                          size="sm"
                          className="inline-action"
                          onClick={() => navigate("versions")}
                        >
                          Open System / Versions
                        </Button>
                      </>
                    )}
                  </p>
                  <p
                    title={
                      status?.manager_release?.build_id ||
                      "Manager build unknown"
                    }
                  >
                    Manager release{" "}
                    {status?.manager_release?.product_version || "unknown"} ·{" "}
                    {shortBuildId(status?.manager_release?.build_id)}
                  </p>
                  {adapters.map((info) => (
                    <p key={info.id}>
                      {info.name} · {runtimeName(info.runtime_type)}:{" "}
                      {!info.enabled
                        ? "disabled"
                        : info.healthy
                          ? "healthy"
                          : "unavailable"}
                    </p>
                  ))}
                </div>
                {!diagnosticsUnavailable &&
                  (diagnostics?.checks || []).some(
                    (check) => check.status === "warning",
                  ) && (
                    <div className="diagnostic-warnings">
                      <strong>Warnings</strong>
                      {(diagnostics?.checks || [])
                        .filter((check) => check.status === "warning")
                        .slice(0, 5)
                        .map((check) => (
                          <div key={check.id}>
                            <span>{check.summary}</span>
                            <Button
                              variant="link"
                              size="sm"
                              className="inline-action"
                              onClick={() =>
                                navigate(
                                  diagnosticDestination(check, check.code),
                                )
                              }
                            >
                              Open
                            </Button>
                          </div>
                        ))}
                    </div>
                  )}
              </section>
              <div className="overview-columns">
                <section className="surface-panel">
                  <SectionHeading
                    title="Access path"
                    description={
                      diagnosticsUnavailable
                        ? "Completion is unavailable until diagnostics refresh."
                        : `${setup.filter((s) => s.done).length} of ${setup.length} checks confirmed`
                    }
                  />
                  <div className="setup-list">
                    {setup.map((step, index) => (
                      <button
                        type="button"
                        className={`setup-item ${step.done ? "done" : ""}`}
                        onClick={() => navigate(step.target)}
                        key={step.title}
                      >
                        <span className="setup-number">
                          {diagnosticsUnavailable ? (
                            "—"
                          ) : step.done ? (
                            <Check size={15} />
                          ) : (
                            index + 1
                          )}
                        </span>
                        <span>
                          <strong>{step.title}</strong>
                          <small>{step.text}</small>
                        </span>
                        <ArrowRight size={16} />
                      </button>
                    ))}
                  </div>
                </section>
                <section className="surface-panel">
                  <SectionHeading
                    title="Recent runs"
                    description="Progress and requests from your agents"
                    action={
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => navigate("runs")}
                      >
                        All runs <ArrowRight size={14} />
                      </Button>
                    }
                  />
                  {runs.length ? (
                    runs
                      .slice(0, 3)
                      .map((run) => (
                        <RunCard
                          key={run.run_id}
                          run={run}
                          compact
                          onOpen={setRunInspect}
                        />
                      ))
                  ) : (
                    <Empty title="No runs yet">
                      Prepared handoffs will appear here when an agent starts.
                    </Empty>
                  )}
                </section>
              </div>
            </div>
          )}
          {section === "nodes" && (
            <div className="nodes-page">
              <div className="list-toolbar page-toolbar">
                <Button
                  onClick={() => {
                    setNodeTest("");
                    setNodeDialog({ mode: "create" });
                  }}
                >
                  <Plus size={16} /> Add Node
                </Button>
              </div>
              {nodes.length ? (
                <div className="node-list">
                  {nodes.map((node) => {
                    const nodeAdapters = adapters.filter(
                      (adapter) => adapter.node_id === node.id,
                    );
                    return (
                      <section
                        className="surface-panel node-card"
                        key={node.id}
                      >
                        <div className="node-card-head">
                          <div className="node-identity">
                            <span className="node-symbol">
                              <Server size={20} />
                            </span>
                            <div className="node-title">
                              <div className="adapter-heading">
                                <h3>{node.name}</h3>
                                <StateBadge
                                  value={
                                    node.health ||
                                    (node.enabled ? "Unobserved" : "Disabled")
                                  }
                                />
                              </div>
                              <p className="path-text">{node.base_url}</p>
                            </div>
                          </div>
                          <div className="row-actions">
                            <Button
                              variant="outline"
                              onClick={() => {
                                setNodeTest("");
                                setNodeDialog({ mode: "edit", node });
                              }}
                            >
                              Edit
                            </Button>
                            <Button
                              onClick={() => {
                                setAdapterTest("");
                                setAdapterDialog({
                                  mode: "create",
                                  nodeId: node.id,
                                });
                              }}
                            >
                              <Plus size={14} /> Add adapter
                            </Button>
                          </div>
                        </div>
                        <div className="node-card-body">
                          <dl className="node-spec">
                            <div>
                              <dt>Endpoint</dt>
                              <dd>{node.base_url}</dd>
                            </div>
                            <div>
                              <dt>Node Protocol</dt>
                              <dd>
                                {node.protocol
                                  ? `v${node.protocol}`
                                  : "Not observed"}
                              </dd>
                            </div>
                            <div>
                              <dt>Node version</dt>
                              <dd>{node.node_version || "Not observed"}</dd>
                            </div>
                            <div>
                              <dt>Release</dt>
                              <dd
                                title={
                                  node.release?.build_id || "Node build unknown"
                                }
                              >
                                {node.release
                                  ? `${node.release.product_version} · ${shortBuildId(node.release.build_id)}`
                                  : "Not observed"}
                              </dd>
                            </div>
                            <div>
                              <dt>Allowed roots</dt>
                              <dd>{node.allowed_root_count ?? 0}</dd>
                            </div>
                            <div>
                              <dt>Capabilities</dt>
                              <dd>
                                {node.capabilities?.length
                                  ? node.capabilities.join(", ")
                                  : "Not observed"}
                              </dd>
                            </div>
                          </dl>
                          <div className="node-adapters">
                            <h4>Runtime adapters</h4>
                            {nodeAdapters.length ? (
                              nodeAdapters.map((adapter) => (
                                <div
                                  className="node-adapter-row"
                                  key={adapter.id}
                                >
                                  <strong>
                                    {adapter.name} ·{" "}
                                    {runtimeName(adapter.runtime_type)}
                                  </strong>
                                  <span className="node-adapter-fact">
                                    {adapter.has_token
                                      ? "Token saved"
                                      : "Token required"}
                                  </span>
                                  <StateBadge
                                    value={
                                      adapter.enabled ? "Enabled" : "Disabled"
                                    }
                                  />
                                  <Button
                                    variant="link"
                                    size="sm"
                                    className="inline-action"
                                    onClick={() =>
                                      setAdapterDialog({
                                        mode: "edit",
                                        adapter,
                                      })
                                    }
                                  >
                                    Edit
                                  </Button>
                                </div>
                              ))
                            ) : (
                              <p className="muted-note">
                                No runtime adapters configured. Add one to make
                                this Node usable for execution targets.
                              </p>
                            )}
                          </div>
                        </div>
                        <div className="node-card-foot">
                          <small>Node ID: {node.id}</small>
                          <small>
                            Node token {node.has_token ? "saved" : "required"}
                          </small>
                        </div>
                      </section>
                    );
                  })}
                </div>
              ) : (
                <Empty title="No Nodes configured">
                  Add the local Mac or another private data-plane service before
                  creating a workspace.
                </Empty>
              )}
            </div>
          )}
          {section === "workspaces" && (
            <div className="workspaces-page">
              <div className="list-toolbar">
                <div className="search-box">
                  <Search size={17} />
                  <Input
                    id="workspace-search"
                    aria-label="Find a workspace"
                    aria-keyshortcuts="/"
                    placeholder="Search workspaces"
                    value={workspaceQuery}
                    onChange={(e) => setWorkspaceQuery(e.target.value)}
                  />
                </div>
                <Button onClick={() => setAddOpen(true)}>
                  <Plus size={16} /> Add workspace
                </Button>
              </div>
              <div className="workspace-count">
                {filtered.length} workspace{filtered.length === 1 ? "" : "s"}
              </div>
              {filtered.length ? (
                <div className="workspace-list">
                  {filtered.map((ws) => (
                    <WorkspaceCard
                      key={`${ws.id}:${ws.excludes}:${ws.write_scope}`}
                      ws={ws}
                      diagnostics={diagnostics}
                      diagnosticsUnavailable={diagnosticsUnavailable}
                      onManage={manage}
                      onRoute={changeRoute}
                      onProfile={(w, runtime) => void openProfile(w, runtime)}
                      onHandoffs={(w) => void openHandoffs(w)}
                      onNavigate={navigate}
                      onConfirm={ask}
                      onNotice={notify}
                      onDetail={setTextDetail}
                    />
                  ))}
                </div>
              ) : (
                <Empty
                  title={
                    workspaceQuery
                      ? "No matching workspaces"
                      : "No workspaces yet"
                  }
                >
                  {workspaceQuery
                    ? "Try a different name or directory."
                    : "Add a project directory to make it available to the Bridge."}
                </Empty>
              )}
            </div>
          )}
          {section === "handoffs" && (
            <div className="handoffs-page">
              <div className="handoff-toolbar">
                <Label htmlFor="handoff-workspace">Workspace</Label>
                <select
                  id="handoff-workspace"
                  className="native-select"
                  value={selectedWorkspace || ""}
                  onChange={(e) => {
                    const ws = workspaces.find((w) => w.id === e.target.value);
                    if (ws) void openHandoffs(ws);
                  }}
                >
                  <option value="">Choose a workspace</option>
                  {workspaces.map((w) => (
                    <option value={w.id} key={w.id}>
                      {w.name}
                    </option>
                  ))}
                </select>
              </div>
              {!selected ? (
                <Empty title="Choose a workspace">
                  Select a workspace to see its prepared handoffs and runs.
                </Empty>
              ) : (
                <>
                  <div className="surface-panel">
                    <SectionHeading
                      title="Prepared handoffs"
                      description={`In ${selected.name}`}
                    />
                    <div className="handoff-route-panel">
                      <div className="handoff-route-picker">
                        <Label htmlFor="handoff-route">Execution target</Label>
                        <select
                          id="handoff-route"
                          className="native-select"
                          value={effectiveSelectedRouteRuntime || ""}
                          onChange={(event) =>
                            setSelectedRouteRuntime(event.target.value || null)
                          }
                          disabled={!selectedRoutes.length}
                        >
                          <option value="">
                            {!selectedRoutes.length
                              ? diagnosticsUnavailable
                                ? "Diagnostics unavailable"
                                : "No canonical route reported"
                              : configuredDefaultRoute &&
                                  !configuredDefaultRoute.ready
                                ? "Default route is blocked"
                                : readySelectedRoutes.length > 1
                                  ? "Choose a ready route"
                                  : "Choose an execution target"}
                          </option>
                          {selectedRoutes.map((route) => (
                            <option key={route.id} value={route.adapter_id}>
                              {route.adapter_name} ·{" "}
                              {runtimeName(route.runtime_type)}
                              {route.is_default ? " · Default" : ""} —{" "}
                              {diagnosticsUnavailable
                                ? "Diagnostics unavailable"
                                : route.ready
                                  ? "Ready"
                                  : "Blocked"}
                            </option>
                          ))}
                        </select>
                      </div>
                      {selectedRoute ? (
                        <RouteSummary
                          route={selectedRoute}
                          report={diagnostics}
                          unavailable={diagnosticsUnavailable}
                          security={securityDetail(
                            selectedWorkspaceRecord?.routes?.[
                              selectedRoute.adapter_id
                            ],
                          )}
                          onNavigate={navigate}
                        />
                      ) : (
                        <p className="diagnostic-unavailable">
                          {diagnosticsUnavailable
                            ? "Route readiness is unavailable. Start is disabled until diagnostics refresh."
                            : "No diagnostic route is available for this workspace."}
                        </p>
                      )}
                    </div>
                    {handoffs.length ? (
                      handoffs.map((h) => (
                        <div className="handoff-row" key={h.id}>
                          <div>
                            <StateBadge
                              value={
                                h.state === "prepared" ? "Published" : h.state
                              }
                            />
                            <h3>{h.title}</h3>
                            <p className="path-text">{h.path}</p>
                          </div>
                          <div className="row-actions">
                            {h.state === "prepared" && (
                              <Button
                                size="sm"
                                disabled={
                                  !selectedRoute ||
                                  diagnosticsUnavailable ||
                                  selectedRoute.ready !== true ||
                                  Boolean(startingHandoff)
                                }
                                onClick={() =>
                                  selectedRoute &&
                                  void startPreparedHandoff(h, selectedRoute)
                                }
                              >
                                <Play size={14} />
                                {startingHandoff ===
                                `${selectedWorkspace}:${h.id}:${selectedRoute?.adapter_id}`
                                  ? "Starting…"
                                  : "Start run"}
                              </Button>
                            )}
                            <Button
                              variant="outline"
                              size="sm"
                              onClick={async () => {
                                try {
                                  await navigator.clipboard.writeText(
                                    h.copy_prompt,
                                  );
                                  notify("Agent prompt copied.");
                                } catch {
                                  setTextDetail({
                                    title: "Copy agent prompt",
                                    content: h.copy_prompt,
                                  });
                                }
                              }}
                            >
                              <Clipboard size={14} /> Copy prompt
                            </Button>
                            {(
                              [
                                "TASK.md",
                                "CONTEXT.md",
                                "ACCEPTANCE.md",
                              ] as const
                            ).map((doc) => (
                              <Button
                                variant="ghost"
                                size="sm"
                                key={doc}
                                onClick={async () => {
                                  try {
                                    const data = await api<{
                                      content?: unknown;
                                    }>(
                                      `/api/workspaces/${selected.id}/document?${new URLSearchParams({ job_id: h.id, document: doc })}`,
                                    );
                                    setTextDetail({
                                      title: `${h.title} — ${doc}`,
                                      content:
                                        typeof data?.content === "string"
                                          ? data.content
                                          : (data?.content ?? data),
                                    });
                                  } catch (error) {
                                    notify((error as Error).message);
                                  }
                                }}
                              >
                                {doc.replace(".md", "")}
                              </Button>
                            ))}
                          </div>
                        </div>
                      ))
                    ) : (
                      <Empty title="No handoffs yet">
                        Ask ChatGPT to inspect this project and prepare a
                        handoff.
                      </Empty>
                    )}
                  </div>
                  <div className="surface-panel">
                    <SectionHeading title="Runs in this workspace" />
                    {workspaceRuns.length ? (
                      workspaceRuns.map((run) => (
                        <RunCard
                          key={run.run_id}
                          run={run}
                          onOpen={setRunInspect}
                        />
                      ))
                    ) : (
                      <Empty title="No runs yet">
                        Start a prepared handoff to see its progress here.
                      </Empty>
                    )}
                  </div>
                </>
              )}
            </div>
          )}
          {section === "runs" && (
            <div className="surface-panel runs-page">
              <SectionHeading
                title="Agent runs"
                description="Open a run to inspect activity or answer a request"
                action={
                  <span className="subtle-count">{runs.length} shown</span>
                }
              />
              {runs.length ? (
                runs.map((run) => (
                  <RunCard key={run.run_id} run={run} onOpen={setRunInspect} />
                ))
              ) : (
                <Empty title="No runs yet">
                  Start an agent from a prepared handoff to see its progress
                  here.
                </Empty>
              )}
              {runsNext !== null && (
                <div className="load-more">
                  <Button
                    variant="outline"
                    onClick={() =>
                      void loadRuns(runsNext).catch((error) =>
                        notify((error as Error).message),
                      )
                    }
                  >
                    Load more runs
                  </Button>
                </div>
              )}
            </div>
          )}
          {section === "adapters" && (
            <div className="runtimes-page">
              <div className="list-toolbar page-toolbar">
                <Button
                  onClick={() => {
                    setAdapterTest("");
                    setAdapterDialog({ mode: "create" });
                  }}
                >
                  <Plus size={16} /> Add adapter
                </Button>
              </div>
              <div className="adapter-list">
                {adapters.map((info) => {
                  const policy = policies[info.id];
                  const health = !info.enabled
                    ? "Disabled"
                    : info.healthy
                      ? "Healthy"
                      : info.healthy === false
                        ? "Unavailable"
                        : "Not observed";
                  return (
                    <section
                      className="surface-panel adapter-card"
                      key={info.id}
                    >
                      <div className="adapter-card-head">
                        <div className="node-identity">
                          <span className="node-symbol">
                            <Command size={20} />
                          </span>
                          <div className="node-title">
                            <div className="adapter-heading">
                              <h3>
                                {info.name} · {runtimeName(info.runtime_type)}
                              </h3>
                              <StateBadge value={health} />
                            </div>
                            <p className="path-text">
                              {info.base_url ||
                                "Endpoint is private to the Node"}
                            </p>
                          </div>
                        </div>
                        <div className="adapter-actions">
                          <Button
                            variant="outline"
                            onClick={() => {
                              setAdapterTest("");
                              setAdapterDialog({ mode: "edit", adapter: info });
                            }}
                          >
                            Edit
                          </Button>
                          <Button
                            variant="outline"
                            onClick={() =>
                              ask({
                                title: `Delete ${info.name}?`,
                                description:
                                  "Deletion is available only when no workspace route or execution history references this adapter.",
                                destructive: true,
                                action: async () => {
                                  await api(
                                    `/api/adapters/${info.id}`,
                                    "DELETE",
                                  );
                                  await refresh();
                                  notify("Adapter deleted.");
                                },
                              })
                            }
                          >
                            Delete
                          </Button>
                        </div>
                      </div>
                      <div className="node-card-body">
                        <dl className="node-spec">
                          <div>
                            <dt>Node</dt>
                            <dd>{info.node_name || "Unknown Node"}</dd>
                          </div>
                          <div>
                            <dt>Endpoint</dt>
                            <dd>{info.base_url || "Private to the Node"}</dd>
                          </div>
                          <div>
                            <dt>Adapter version</dt>
                            <dd>{info.adapter_version || "Unknown"}</dd>
                          </div>
                          <div>
                            <dt>Native version</dt>
                            <dd>{info.native_version || "Not observed"}</dd>
                          </div>
                          <div>
                            <dt>Release</dt>
                            <dd
                              title={
                                info.release?.build_id ||
                                "Adapter build unknown"
                              }
                            >
                              {info.release
                                ? `${info.release.product_version} · ${shortBuildId(info.release.build_id)}`
                                : "Not observed"}
                            </dd>
                          </div>
                          <div>
                            <dt>Node token</dt>
                            <dd>
                              {info.has_token
                                ? "Token saved"
                                : "Token required"}
                            </dd>
                          </div>
                        </dl>
                        <div className="adapter-policy-block">
                          <h4>Model policy</h4>
                          {policy?.configured ? (
                            <p className="adapter-policy-summary">
                              <strong>
                                {policy.enabled?.length || 0} model
                                {(policy.enabled?.length || 0) === 1
                                  ? ""
                                  : "s"}{" "}
                                enabled
                              </strong>
                              <span>Default {policy.default || "not set"}</span>
                            </p>
                          ) : (
                            <p className="adapter-policy-empty">
                              No model policy configured. Models are
                              unrestricted by Bridge policy; the runtime picks
                              its default unless a run requests a specific
                              model.
                            </p>
                          )}
                          <div className="adapter-policy-cta">
                            <Button
                              variant="outline"
                              size="sm"
                              onClick={() => setModelAdapterId(info.id)}
                            >
                              Models
                            </Button>
                          </div>
                        </div>
                        {(info.runtime_type === "codex" ||
                          info.runtime_type === "claude") &&
                          info.features?.usageLimits === 1 && (
                            <div className="adapter-quota-block">
                              <h4>
                                {info.runtime_type === "claude"
                                  ? "Claude quota (last run)"
                                  : "Codex quota"}
                              </h4>
                              {(() => {
                                const limits = usageLimits[info.id];
                                if (!info.enabled || info.healthy === false)
                                  return null;
                                if (limits === undefined)
                                  return (
                                    <p className="adapter-quota-status">
                                      Loading quota…
                                    </p>
                                  );
                                // ordinaryUsageAllowed=false is an explicit
                                // backend state and is surfaced even when no
                                // percentage window rows exist; no 0% is
                                // invented when windows are absent.
                                const ordinaryFalse =
                                  limits !== null &&
                                  limits.available &&
                                  limits.ordinaryUsageAllowed === false;
                                if (
                                  limits === null ||
                                  !limits.available ||
                                  limits.buckets.length === 0
                                )
                                  return (
                                    <p className="adapter-quota-status">
                                      {ordinaryFalse
                                        ? "Ordinary usage unavailable."
                                        : info.runtime_type === "claude"
                                          ? "No quota observed yet. Claude Code reports usage while a run is active."
                                          : "Quota unavailable. The runtime could not report the current account balance."}
                                    </p>
                                  );
                                // Rows are the normalized windows inside each
                                // bucket (primary then secondary, order from
                                // the native fields; labels come only from
                                // windowDurationMins — never assumed 5h/7d).
                                // Buckets without windows contribute no row.
                                const rows = limits.buckets
                                  .slice(0, 4)
                                  .flatMap((bucket, bucketIndex) =>
                                    (bucket.windows || [])
                                      .slice(0, 4)
                                      .map((window, windowIndex) => ({
                                        bucket,
                                        window,
                                        bucketIndex,
                                        windowIndex,
                                      })),
                                  )
                                  .slice(0, 4);
                                if (!rows.length)
                                  return (
                                    <p className="adapter-quota-status">
                                      {ordinaryFalse
                                        ? "Ordinary usage unavailable."
                                        : info.runtime_type === "claude"
                                          ? "No quota observed yet. Claude Code reports usage while a run is active."
                                          : "Quota unavailable. The runtime could not report the current account balance."}
                                    </p>
                                  );
                                return (
                                  <div className="quota-rows">
                                    {ordinaryFalse && (
                                      <p className="adapter-quota-status">
                                        Ordinary usage unavailable.
                                      </p>
                                    )}
                                    {rows.map(
                                      ({
                                        bucket,
                                        window,
                                        bucketIndex,
                                        windowIndex,
                                      }) => {
                                        // Bucket identity is the native quota-bucket label: limitName
                                        // first, then limitId. Duration identity comes only from
                                        // windowDurationMins; primary/secondary meanings and any
                                        // model mapping are never inferred.
                                        const bucketIdentity =
                                          bucket.limitName || bucket.limitId;
                                        const durationIdentity =
                                          quotaWindowLabel(
                                            window.windowDurationMins,
                                          );
                                        const label =
                                          bucketIdentity && durationIdentity
                                            ? `${bucketIdentity} · ${durationIdentity}`
                                            : bucketIdentity ||
                                              durationIdentity ||
                                              `Window ${windowIndex + 1}`;
                                        const keyed = `${bucket.limitId || bucket.limitName || bucketIndex}:${window.windowDurationMins ?? windowIndex}:${windowIndex}`;
                                        return (
                                          <div
                                            className="quota-row"
                                            key={keyed}
                                          >
                                            <span className="quota-label">
                                              {label}
                                            </span>
                                            <div
                                              className="quota-bar"
                                              role="meter"
                                              aria-valuemin={0}
                                              aria-valuemax={100}
                                              aria-valuenow={
                                                window.remainingPercent
                                              }
                                              aria-label={`${label} remaining percent`}
                                            >
                                              <span
                                                className="quota-fill"
                                                style={{
                                                  width: `${window.remainingPercent}%`,
                                                }}
                                              />
                                            </div>
                                            <strong className="quota-remaining">
                                              {window.remainingPercent}% left
                                            </strong>
                                            <small className="quota-reset">
                                              {quotaResetText(window.resetsAt)}
                                            </small>
                                          </div>
                                        );
                                      },
                                    )}
                                  </div>
                                );
                              })()}
                            </div>
                          )}
                      </div>
                      <div className="node-card-foot">
                        <small>Adapter ID: {info.id}</small>
                      </div>
                    </section>
                  );
                })}
                {!adapters.length && (
                  <Empty title="No adapters configured">
                    Add Local Pi, GPU Pi, Codex, or Claude Code as separate
                    execution targets.
                  </Empty>
                )}
              </div>
              {adapters.length > 0 && (
                <section className="surface-panel adapter-detail-panel">
                  <SectionHeading
                    title="Adapter security profiles"
                    description="Native profiles are discovered separately for each adapter and workspace."
                  />
                  <ProfileManager
                    adapters={adapters}
                    workspaces={workspaces}
                    onChanged={refresh}
                    notify={notify}
                  />
                </section>
              )}
              <div className="connection-panel">
                <div className="connection-head">
                  <div className="connection-symbol">
                    <Workflow size={21} />
                  </div>
                  <div>
                    <h3>Bridge connection</h3>
                    <p>One connection serves all enabled workspaces.</p>
                  </div>
                  <StateBadge
                    value={
                      !status?.bridge.configured
                        ? "Gateway not configured"
                        : status.bridge.enabled
                          ? "Gateway enabled"
                          : "Gateway disabled"
                    }
                  />
                </div>
                <p className="external-connection-note">
                  External ChatGPT and tunnel connection: not observed by this
                  service.
                </p>
                <div className="connection-endpoint">
                  MCP endpoint{" "}
                  <code>http://127.0.0.1:{status?.mcp_port}/mcp</code>
                </div>
                <div className="connection-actions">
                  <Button
                    variant="outline"
                    onClick={() =>
                      setTextDetail({
                        title: "Tunnel setup",
                        content: `config_version: 1\ncontrol_plane:\n  tunnel_id: tunnel_REPLACE_WITH_YOUR_32_HEX_ID\n  api_key: env:CONTROL_PLANE_API_KEY\nmcp:\n  server_urls:\n    - channel: main\n      url: http://127.0.0.1:${status?.mcp_port}/mcp\n  extra_headers:\n    X-Bridge-Token: env:WORKSPACE_BRIDGE_TOKEN\n  discovery_extra_headers:\n    X-Bridge-Token: env:WORKSPACE_BRIDGE_TOKEN\nhealth:\n  listen_addr: 127.0.0.1:8790\n\nSave outside your project as bridge-tunnel.yaml. Supply keys locally, then run tunnel-client doctor and tunnel-client run.`,
                      })
                    }
                  >
                    Tunnel setup
                  </Button>
                  <Button
                    variant="outline"
                    onClick={() =>
                      ask({
                        title: status?.bridge.configured
                          ? "Rotate bridge token?"
                          : "Create bridge token?",
                        description:
                          "The new credential authorizes all enabled workspaces. The previous token is revoked. Save the new token when it appears.",
                        destructive: true,
                        action: () => bridgeChange("rotate_token"),
                      })
                    }
                  >
                    {status?.bridge.configured
                      ? "Rotate token"
                      : "Create token"}
                  </Button>
                  <Button
                    variant="outline"
                    disabled={!status?.bridge.configured}
                    onClick={() =>
                      ask({
                        title: status?.bridge.enabled
                          ? "Pause MCP access?"
                          : "Enable MCP access?",
                        description: status?.bridge.enabled
                          ? "All workspace access through this Bridge will pause."
                          : "Enabled workspaces become accessible through the shared connection.",
                        action: () =>
                          bridgeChange(
                            status?.bridge.enabled ? "disable" : "enable",
                          ),
                      })
                    }
                  >
                    {status?.bridge.enabled ? (
                      <Pause size={15} />
                    ) : (
                      <Play size={15} />
                    )}
                    {status?.bridge.enabled ? "Pause access" : "Enable access"}
                  </Button>
                </div>
              </div>
            </div>
          )}
          {section === "versions" && (
            <div className="surface-panel updates-page">
              <SectionHeading
                title="Installed components"
                description="See what is installed and whether each component can work with this Bridge."
              />
              {versionsError ? (
                <div className="diagnostic-unavailable" role="status">
                  <strong>Versions status is unavailable.</strong>
                  <p>{versionsError}</p>
                </div>
              ) : !versions ? (
                <p className="version-loading" role="status">
                  Loading versions status…
                </p>
              ) : versions.status === "failed" ? (
                <div className="diagnostic-unavailable" role="status">
                  <strong>Versions status is unavailable.</strong>
                  <p>
                    {versions.error?.summary ||
                      "Topology could not be observed. Existing routes stay usable where their Node and adapter remain reachable."}
                  </p>
                </div>
              ) : (
                <>
                  <div className="version-target">
                    <div>
                      <span>Target Bridge version</span>
                      <strong>
                        {versions.target.product_version || "Unknown"}
                      </strong>
                    </div>
                    <p>
                      Compatible older Nodes and adapters can keep running.
                      Version differences alone do not block routes.
                    </p>
                  </div>
                  <div className="version-groups">
                    <VersionGroup
                      title="Bridge and Manager"
                      entries={[versions.bridge, versions.manager].filter(
                        (entry): entry is VersionComponentState =>
                          Boolean(entry),
                      )}
                      empty="Bridge and Manager versions are unavailable."
                    />
                    <VersionGroup
                      title="Nodes"
                      entries={versions.nodes || []}
                      empty="No Nodes configured."
                      names={Object.fromEntries(
                        nodes.map((node) => [node.id, node.name]),
                      )}
                    />
                    <VersionGroup
                      title="Adapters"
                      entries={versions.adapters || []}
                      empty="No adapters configured."
                      names={Object.fromEntries(
                        adapters.map((adapter) => [adapter.id, adapter.name]),
                      )}
                    />
                  </div>
                  <section className="version-guidance">
                    <h3>Updating components</h3>
                    <p>
                      Informational only. This view never installs, restarts, or
                      rolls back anything. Update on each component’s host when
                      convenient, then restart the affected service explicitly.
                    </p>
                    <div className="version-guidance-grid">
                      <div>
                        <strong>
                          Bridge, Node, Codex and Claude Code adapters
                        </strong>
                        <p>
                          Upgrade the local Python package with{" "}
                          <code>uv tool upgrade workspace-bridge</code>.
                        </p>
                      </div>
                      <div>
                        <strong>Pi adapter</strong>
                        <p>
                          Upgrade the local npm package{" "}
                          <code>workspace-bridge-pi-host-adapter</code> on its
                          host.
                        </p>
                      </div>
                    </div>
                  </section>
                </>
              )}
            </div>
          )}
          {section === "audit" && (
            <div className="surface-panel audit-page">
              <SectionHeading
                title="Recent activity"
                description="Access and configuration changes. File content is never logged."
              />
              {events.length ? (
                <div className="audit-list">
                  {events.map((event, index) => (
                    <div className="audit-row" key={`${event.at}-${index}`}>
                      <span className="audit-timeline" />
                      <div>
                        <strong>{event.action}</strong>
                        <p>
                          {workspaces.find((w) => w.id === event.workspace)
                            ?.name || "Admin"}{" "}
                          <span aria-hidden="true">/</span> {event.outcome}
                        </p>
                        {(event.node_name ||
                          event.node_id ||
                          event.adapter_name) && (
                          <small className="path-text">
                            Node:{" "}
                            {event.node_name || event.node_id || "Unknown"}
                            {event.adapter_name
                              ? ` · ${event.adapter_name}${event.runtime_type ? ` · ${runtimeName(event.runtime_type)}` : ""}`
                              : ""}
                          </small>
                        )}
                      </div>
                      <time dateTime={event.at}>{dateTime(event.at)}</time>
                    </div>
                  ))}
                </div>
              ) : (
                <Empty title="No activity yet">
                  Access and configuration changes will appear here.
                </Empty>
              )}
            </div>
          )}
        </main>
        <footer className="statusline" aria-label="Console status">
          <span className="statusline-mode">
            {refreshing ? "SYNC" : "LIVE"}
          </span>
          <span className="statusline-path">~/{page.id}</span>
          <span
            className={`statusline-gateway ${
              status?.bridge.configured && status.bridge.enabled
                ? "is-on"
                : "is-off"
            }`}
          >
            gateway{" "}
            {!status?.bridge.configured
              ? "unset"
              : status.bridge.enabled
                ? "on"
                : "paused"}
          </span>
          {attentionRuns.length > 0 && (
            <span className="statusline-attention">
              {attentionRuns.length} waiting
            </span>
          )}
          <span className="statusline-spacer" />
          <span>auto {autoRefresh ? "30s" : "off"}</span>
          {lastUpdated && <span>sync {relativeTime(lastUpdated, now)}</span>}
          <span className="statusline-hint">
            <kbd>ctrl k</kbd> commands
          </span>
        </footer>
      </div>
      <CommandPalette
        open={paletteOpen}
        onOpenChange={setPaletteOpen}
        commands={commands}
      />
      <Sheet open={mobileNav} onOpenChange={setMobileNav}>
        <SheetContent side="left" className="mobile-sheet">
          <SheetHeader>
            <SheetTitle>Workspace Bridge</SheetTitle>
            <SheetDescription>Local agent operations</SheetDescription>
          </SheetHeader>
          <nav aria-label="Mobile navigation">
            {sections.map((s) => (
              <button
                type="button"
                key={s.id}
                className={`nav-item ${section === s.id ? "current" : ""}`}
                onClick={() => navigate(s.id)}
              >
                <s.icon size={17} />
                {s.title}
              </button>
            ))}
          </nav>
        </SheetContent>
      </Sheet>
      <Dialog open={addOpen} onOpenChange={setAddOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Add a workspace</DialogTitle>
            <DialogDescription>
              Choose a project directory. Access starts disabled.
            </DialogDescription>
          </DialogHeader>
          <form className="dialog-form" onSubmit={addWorkspace}>
            <div className="form-field">
              <Label htmlFor="add-name">Name</Label>
              <Input
                id="add-name"
                name="name"
                maxLength={80}
                placeholder="My project"
                required
              />
            </div>
            <div className="form-field">
              <Label htmlFor="add-node">Authoritative Node</Label>
              <select
                id="add-node"
                className="native-select"
                name="node_id"
                required
              >
                <option value="">Choose the machine that owns this root</option>
                {nodes.map((node) => (
                  <option
                    key={node.id}
                    value={node.id}
                    disabled={!node.enabled}
                  >
                    {node.name} · {node.health || "unobserved"}
                  </option>
                ))}
              </select>
              <small>
                Workspace files, Git, handoffs, and runs use this Node.
              </small>
            </div>
            <div className="form-field">
              <Label htmlFor="add-root">Project directory</Label>
              <Input
                id="add-root"
                name="root"
                placeholder="/Users/you/Projects/my-project"
                required
              />
              <small>
                Enter a canonical absolute path on the selected Node.
              </small>
            </div>
            <div className="form-field">
              <Label htmlFor="add-excludes">Extra exclusions</Label>
              <Textarea
                id="add-excludes"
                name="excludes"
                rows={3}
                placeholder={"private/**\nfixtures/large/**"}
              />
            </div>
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => setAddOpen(false)}
              >
                Cancel
              </Button>
              <Button type="submit">Add workspace</Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
      {nodeDialog && (
        <Dialog open onOpenChange={(open) => !open && setNodeDialog(null)}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>
                {nodeDialog.mode === "create"
                  ? "Add Node"
                  : `Edit ${nodeDialog.node?.name}`}
              </DialogTitle>
              <DialogDescription>
                Node credentials stay in the private Bridge state and are never
                returned after save. Allowed roots are configured on the Node
                host.
              </DialogDescription>
            </DialogHeader>
            <form className="dialog-form" onSubmit={saveNode}>
              <div className="form-field">
                <Label htmlFor="node-name">Name</Label>
                <Input
                  id="node-name"
                  name="name"
                  defaultValue={nodeDialog.node?.name || ""}
                  required
                  maxLength={80}
                  placeholder="Local Mac"
                />
              </div>
              <div className="form-field">
                <Label htmlFor="node-url">Node URL</Label>
                <Input
                  id="node-url"
                  name="base_url"
                  defaultValue={nodeDialog.node?.base_url || ""}
                  required
                  maxLength={2048}
                  placeholder="http://127.0.0.1:8770"
                />
              </div>
              <div className="form-field">
                <Label htmlFor="node-token">Node token</Label>
                <Input
                  id="node-token"
                  name="token"
                  type="password"
                  autoComplete="new-password"
                  defaultValue=""
                  placeholder={
                    nodeDialog.mode === "create"
                      ? "Required"
                      : "Blank keeps the saved token"
                  }
                  required={nodeDialog.mode === "create"}
                  maxLength={4096}
                />
                <small>
                  Leave blank while editing to preserve the saved token.
                </small>
              </div>
              <label className="adapter-enabled-field">
                <input
                  name="enabled"
                  type="checkbox"
                  defaultChecked={nodeDialog.node?.enabled ?? true}
                />{" "}
                Node enabled
              </label>
              {nodeTest && (
                <p className="adapter-test-result" role="status">
                  {nodeTest}
                </p>
              )}
              <DialogFooter>
                <Button
                  type="button"
                  variant="outline"
                  onClick={(event) =>
                    void testNode(event.currentTarget.form || undefined)
                  }
                >
                  Test connection
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => setNodeDialog(null)}
                >
                  Cancel
                </Button>
                <Button type="submit">
                  {nodeDialog.mode === "create" ? "Add Node" : "Save Node"}
                </Button>
              </DialogFooter>
            </form>
          </DialogContent>
        </Dialog>
      )}
      {adapterDialog && (
        <Dialog open onOpenChange={(open) => !open && setAdapterDialog(null)}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>
                {adapterDialog.mode === "create"
                  ? "Add adapter"
                  : `Edit ${adapterDialog.adapter?.name}`}
              </DialogTitle>
              <DialogDescription>
                The selected Node stores and tests this runtime adapter. The
                adapter token is write-only and is never read back.
              </DialogDescription>
            </DialogHeader>
            <form className="dialog-form" onSubmit={saveAdapter}>
              <div className="form-field">
                <Label htmlFor="adapter-node">Authoritative Node</Label>
                {adapterDialog.mode === "create" ? (
                  <select
                    id="adapter-node"
                    className="native-select"
                    name="node_id"
                    defaultValue={adapterDialog.nodeId || nodes[0]?.id || ""}
                    required
                  >
                    <option value="">Choose a Node</option>
                    {nodes.map((node) => (
                      <option key={node.id} value={node.id}>
                        {node.name} · {node.health || "unobserved"}
                      </option>
                    ))}
                  </select>
                ) : (
                  <p className="form-readonly">
                    {adapterDialog.adapter?.node_name || "Node unknown"} · fixed
                    for this adapter
                  </p>
                )}
              </div>
              <div className="form-field">
                <Label htmlFor="adapter-name">Name</Label>
                <Input
                  id="adapter-name"
                  name="name"
                  defaultValue={adapterDialog.adapter?.name || ""}
                  maxLength={80}
                  required
                />
              </div>
              <div className="form-field">
                <Label htmlFor="adapter-type">Runtime type</Label>
                {adapterDialog.mode === "create" ? (
                  <select
                    id="adapter-type"
                    className="native-select"
                    name="runtime_type"
                    defaultValue="pi"
                  >
                    <option value="pi">Pi</option>
                    <option value="codex">Codex</option>
                    <option value="claude">Claude Code</option>
                  </select>
                ) : (
                  <p className="form-readonly">
                    {runtimeName(adapterDialog.adapter?.runtime_type || "pi")} ·
                    fixed for this adapter
                  </p>
                )}
              </div>
              <div className="form-field">
                <Label htmlFor="adapter-url">Base URL</Label>
                <Input
                  id="adapter-url"
                  name="base_url"
                  defaultValue={adapterDialog.adapter?.base_url || ""}
                  placeholder="http://127.0.0.1:8767"
                  maxLength={2048}
                  required
                />
              </div>
              <div className="form-field">
                <Label htmlFor="adapter-token">Adapter token</Label>
                <Input
                  id="adapter-token"
                  name="token"
                  type="password"
                  autoComplete="new-password"
                  defaultValue=""
                  placeholder={
                    adapterDialog.mode === "create"
                      ? "Required"
                      : "Blank keeps the saved token"
                  }
                  required={adapterDialog.mode === "create"}
                  maxLength={4096}
                />
                <small>
                  {adapterDialog.mode === "create"
                    ? "Stored plaintext in the owning Node's private SQLite state (0600); Manager never reads it back."
                    : "Leave blank to preserve the saved token. Enter a new value to replace it."}
                </small>
              </div>
              <label className="adapter-enabled-field">
                <input
                  name="enabled"
                  type="checkbox"
                  defaultChecked={adapterDialog.adapter?.enabled ?? true}
                />{" "}
                Adapter enabled
              </label>
              {adapterTest && (
                <p className="adapter-test-result" role="status">
                  {adapterTest}
                </p>
              )}
              <DialogFooter>
                <Button
                  type="button"
                  variant="outline"
                  onClick={(event) =>
                    void testAdapter(event.currentTarget.form || undefined)
                  }
                >
                  Test connection
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => setAdapterDialog(null)}
                >
                  Cancel
                </Button>
                <Button type="submit">
                  {adapterDialog.mode === "create"
                    ? "Add adapter"
                    : "Save adapter"}
                </Button>
              </DialogFooter>
            </form>
          </DialogContent>
        </Dialog>
      )}
      {profileFor && (
        <ProfileAssignment
          workspace={profileFor.ws}
          adapterId={profileFor.adapterId}
          onClose={() => setProfileFor(null)}
          onManage={() => {
            setProfileFor(null);
            navigate("adapters");
          }}
          onChanged={refresh}
          notify={notify}
        />
      )}
      {modelAdapterId && (
        <ModelDialog
          adapterId={modelAdapterId}
          adapter={adapters.find((item) => item.id === modelAdapterId)}
          open
          onClose={() => setModelAdapterId(null)}
          workspaces={workspaces}
          policy={policies[modelAdapterId]}
          onSaved={refresh}
          onNotice={notify}
        />
      )}
      {runInspect && (
        <RunInspector
          key={runInspect.run_id}
          run={runInspect}
          onClose={() => setRunInspect(null)}
          onNotice={notify}
          onRefresh={refresh}
          onDetail={setTextDetail}
          onConfirm={ask}
        />
      )}
      <Sheet
        open={Boolean(textDetail)}
        onOpenChange={(value) => !value && setTextDetail(null)}
      >
        <SheetContent side="right" className="detail-sheet">
          <SheetHeader>
            <SheetTitle>{textDetail?.title || "Details"}</SheetTitle>
            <SheetDescription>
              {textDetail?.description ||
                "Content shown as text from your local Bridge."}
            </SheetDescription>
          </SheetHeader>
          <pre className="detail-pre">
            {textDetail ? jsonText(textDetail.content) : ""}
          </pre>
        </SheetContent>
      </Sheet>
      <AlertDialog
        open={Boolean(confirm)}
        onOpenChange={(value) => !value && setConfirm(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{confirm?.title}</AlertDialogTitle>
            <AlertDialogDescription>
              {confirm?.description}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              className={confirm?.destructive ? "confirm-danger" : ""}
              onClick={() => void executeConfirmed()}
            >
              Continue
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
      {notice && (
        <div className="notice" role="status">
          <span>{notice}</span>
          <Button
            variant="ghost"
            size="icon-xs"
            aria-label="Dismiss message"
            onClick={() => setNotice("")}
          >
            <X size={14} />
          </Button>
        </div>
      )}
    </div>
  );
}
function Brand() {
  return (
    <div className="brand">
      <span className="bridge-mark" aria-hidden="true">
        <SquareTerminal size={20} strokeWidth={2.2} />
      </span>
      <span>
        <strong>
          workspace<span className="brand-dash">-</span>bridge
        </strong>
        <small>local agent operations</small>
      </span>
    </div>
  );
}

function RunInspector({
  run,
  onClose,
  onNotice,
  onRefresh,
  onDetail,
  onConfirm,
}: {
  run: Run | null;
  onClose: () => void;
  onNotice: (message: string) => void;
  onRefresh: () => Promise<void>;
  onDetail: (detail: TextDetail) => void;
  onConfirm: (state: ConfirmState) => void;
}) {
  const [data, setData] = useState<Run | null>(run);
  const [activity, setActivity] = useState<RunActivity[]>([]);
  const [executions, setExecutions] = useState<ExecutionRecord[]>([]);
  const [activityCursor, setActivityCursor] = useState<FeedCursor | null>(null);
  const [executionCursor, setExecutionCursor] = useState<FeedCursor | null>(
    null,
  );
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [tab, setTab] = useState<"summary" | "activity" | "executions">(
    "summary",
  );
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const panelRef = useRef<HTMLDivElement | null>(null);
  const loadOlderRef = useRef<HTMLDivElement | null>(null);
  const loadingOlderRef = useRef(false);
  const activityOlderLoadedRef = useRef(false);
  const executionsOlderLoadedRef = useRef(false);
  const pullStartY = useRef<number | null>(null);
  const pullDistanceRef = useRef(0);
  const pullRefreshingRef = useRef(false);
  const [pullDistance, setPullDistance] = useState(0);
  const [pullRefreshing, setPullRefreshing] = useState(false);
  const load = useCallback(
    async (id: string) => {
      try {
        const details = await api<Run>(`/api/runs/${id}`);
        setData(details);
        const [acts, execs] = await Promise.allSettled([
          api<ActivityPage>(`/api/runs/${id}/activities?limit=50`),
          api<ExecutionPage>(`/api/runs/${id}/executions?limit=50`),
        ]);
        if (acts.status === "fulfilled") {
          setActivity((old) =>
            mergeNewest(acts.value.activities || [], old, (item) => item.id),
          );
          if (!activityOlderLoadedRef.current) {
            setActivityCursor(acts.value.next_cursor || null);
          }
        }
        if (execs.status === "fulfilled") {
          setExecutions((old) =>
            mergeNewest(
              execs.value.executions || [],
              old,
              (item) => item.execution_id,
            ),
          );
          if (!executionsOlderLoadedRef.current) {
            setExecutionCursor(execs.value.next_cursor || null);
          }
        }
      } catch (error) {
        onNotice((error as Error).message);
      }
    },
    [onNotice],
  );
  const loadOlder = useCallback(
    async (feed: "activity" | "executions") => {
      const cursor = feed === "activity" ? activityCursor : executionCursor;
      if (!run || !cursor || loadingOlderRef.current) return;
      loadingOlderRef.current = true;
      setLoadingOlder(true);
      try {
        const query = new URLSearchParams({
          limit: "50",
          before_created: cursor.created,
          before_id: cursor.id,
        });
        if (feed === "activity") {
          const page = await api<ActivityPage>(
            `/api/runs/${run.run_id}/activities?${query}`,
          );
          setActivity((old) =>
            appendOlder(old, page.activities || [], (item) => item.id),
          );
          setActivityCursor(page.next_cursor || null);
          activityOlderLoadedRef.current = true;
        } else {
          const page = await api<ExecutionPage>(
            `/api/runs/${run.run_id}/executions?${query}`,
          );
          setExecutions((old) =>
            appendOlder(
              old,
              page.executions || [],
              (item) => item.execution_id,
            ),
          );
          setExecutionCursor(page.next_cursor || null);
          executionsOlderLoadedRef.current = true;
        }
      } catch (error) {
        onNotice((error as Error).message);
      } finally {
        loadingOlderRef.current = false;
        setLoadingOlder(false);
      }
    },
    [activityCursor, executionCursor, onNotice, run],
  );
  useEffect(() => {
    if (run) void load(run.run_id);
  }, [run, load]);
  useEffect(() => {
    const cursor = tab === "activity" ? activityCursor : executionCursor;
    const target = loadOlderRef.current;
    const root = panelRef.current;
    if (
      !cursor ||
      loadingOlder ||
      (tab !== "activity" && tab !== "executions") ||
      !target ||
      !root ||
      typeof IntersectionObserver === "undefined"
    ) {
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) void loadOlder(tab);
      },
      { root, rootMargin: "180px 0px" },
    );
    observer.observe(target);
    return () => observer.disconnect();
  }, [activityCursor, executionCursor, loadOlder, loadingOlder, tab]);
  const inspectRunId = run?.run_id;
  useEffect(() => {
    if (!inspectRunId) return;
    const timer = window.setInterval(() => {
      if (document.hidden || pullRefreshingRef.current) return;
      void load(inspectRunId);
    }, autoRefreshMs);
    return () => window.clearInterval(timer);
  }, [inspectRunId, load]);
  useEffect(() => {
    const panel = panelRef.current;
    if (!panel || !run) return;

    const resetPull = () => {
      pullStartY.current = null;
      pullDistanceRef.current = 0;
      setPullDistance(0);
    };
    const handleTouchStart = (event: TouchEvent) => {
      if (
        event.touches.length !== 1 ||
        panel.scrollTop > 0 ||
        pullRefreshingRef.current
      ) {
        pullStartY.current = null;
        return;
      }
      pullStartY.current = event.touches[0].clientY;
    };
    const handleTouchMove = (event: TouchEvent) => {
      const startY = pullStartY.current;
      if (startY === null) return;
      if (event.touches.length !== 1 || panel.scrollTop > 0) {
        resetPull();
        return;
      }
      const distance = event.touches[0].clientY - startY;
      if (distance <= 0) {
        resetPull();
        return;
      }
      if (distance > 6) event.preventDefault();
      const nextDistance = Math.min(distance, 96);
      pullDistanceRef.current = nextDistance;
      setPullDistance(nextDistance);
    };
    const handleTouchEnd = () => {
      pullStartY.current = null;
      const shouldRefresh = pullDistanceRef.current >= 68;
      pullDistanceRef.current = 0;
      setPullDistance(0);
      if (!shouldRefresh || pullRefreshingRef.current) return;

      pullRefreshingRef.current = true;
      setPullRefreshing(true);
      void Promise.allSettled([load(run.run_id), onRefresh()])
        .then(([, pageRefresh]) => {
          if (pageRefresh.status === "rejected")
            onNotice(
              pageRefresh.reason instanceof Error
                ? pageRefresh.reason.message
                : "Unable to refresh page data.",
            );
        })
        .finally(() => {
          pullRefreshingRef.current = false;
          setPullRefreshing(false);
        });
    };

    panel.addEventListener("touchstart", handleTouchStart, { passive: true });
    panel.addEventListener("touchmove", handleTouchMove, { passive: false });
    panel.addEventListener("touchend", handleTouchEnd, { passive: true });
    panel.addEventListener("touchcancel", resetPull, { passive: true });
    return () => {
      panel.removeEventListener("touchstart", handleTouchStart);
      panel.removeEventListener("touchmove", handleTouchMove);
      panel.removeEventListener("touchend", handleTouchEnd);
      panel.removeEventListener("touchcancel", resetPull);
    };
  }, [load, onNotice, onRefresh, run?.run_id]);
  async function replyInteraction(interactionId: string, body: Json) {
    if (!run) return;
    try {
      await api(
        `/api/runs/${run.run_id}/interactions/${interactionId}`,
        "POST",
        body,
      );
      onNotice("Request answered.");
      await load(run.run_id);
      await onRefresh();
    } catch (error) {
      onNotice((error as Error).message);
    }
  }
  if (!run) return null;
  const current = data || run;
  const pending = (current.interactions || []).filter(
    (i) => i.state === "pending",
  );
  return (
    <Sheet open={Boolean(run)} onOpenChange={(value) => !value && onClose()}>
      <SheetContent ref={panelRef} side="right" className="run-sheet">
        <div
          className="run-pull-indicator"
          data-visible={pullRefreshing || pullDistance > 0}
          aria-hidden={!pullRefreshing && pullDistance === 0}
          aria-live="polite"
          aria-atomic="true"
        >
          <span>
            <RefreshCw size={13} className={pullRefreshing ? "spinning" : ""} />
            {pullRefreshing
              ? "Refreshing run…"
              : pullDistance >= 68
                ? "Release to refresh"
                : "Pull down to refresh"}
          </span>
        </div>
        <SheetHeader>
          <div className="inspector-state">
            <StateBadge value={displayState(current)} />{" "}
            <span>
              {current.adapter_name || "Unknown adapter"} ·{" "}
              {runtimeName(current.runtime_type || "unknown")}
            </span>
          </div>
          <SheetTitle>
            {current.handoff_title || current.job_id || "Agent run"}
          </SheetTitle>
          <SheetDescription>
            {current.workspace_name || current.workspace_id || "Workspace"} ·
            started {dateTime(current.created)}
          </SheetDescription>
        </SheetHeader>
        <div className="inspector-tabs" role="tablist" aria-label="Run detail">
          <button
            type="button"
            role="tab"
            aria-selected={tab === "summary"}
            onClick={() => setTab("summary")}
          >
            Summary
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "activity"}
            onClick={() => setTab("activity")}
          >
            Activities
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "executions"}
            onClick={() => setTab("executions")}
          >
            Executions
          </button>
        </div>
        <div className="inspector-body">
          {tab === "summary" && (
            <>
              <div className="run-facts">
                <div>
                  <span>Model</span>
                  <strong>
                    {current.model || "Default"}
                    {current.reasoning ? (
                      <> · {thinkingEffortLabel(current.reasoning)}</>
                    ) : null}
                  </strong>
                </div>
                <div>
                  <span>Conversation</span>
                  <strong>{current.conversation_id || "—"}</strong>
                </div>
                <div>
                  <span>Run ID</span>
                  <strong>{current.run_id}</strong>
                </div>
                <div>
                  <span>Node</span>
                  <strong>
                    {current.node_name || current.node_id || "Unknown"} · rev{" "}
                    {current.node_revision || "—"}
                  </strong>
                </div>
                <div>
                  <span>Adapter</span>
                  <strong>
                    {current.adapter_name || current.adapter_id || "Unknown"} ·
                    rev {current.adapter_revision || "—"}
                  </strong>
                </div>
              </div>
              <section className="request-panel effective-security-panel">
                <StateBadge
                  value={
                    current.effective_security?.source === "runtime-config"
                      ? "Codex runtime config"
                      : current.effective_security?.source === "profile"
                        ? "Bridge profile"
                        : "Security unavailable"
                  }
                />
                <h3>Security used for this run</h3>
                {current.effective_security?.source === "profile" ? (
                  <p>
                    Profile {current.effective_security.profile_id || "unknown"}{" "}
                    · bound revision{" "}
                    {current.effective_security.bound_revision || "unknown"} ·
                    effective revision{" "}
                    {current.effective_security.effective_revision || "unknown"}
                  </p>
                ) : current.effective_security?.source === "runtime-config" ? (
                  <>
                    <p>
                      Bound revision{" "}
                      {current.effective_security.bound_revision || "unknown"} ·
                      effective revision{" "}
                      {current.effective_security.effective_revision ||
                        "unknown"}
                    </p>
                    <p>
                      Active permission profile:{" "}
                      {String(
                        current.effective_security.resolved_summary
                          ?.activePermissionProfile || "unknown",
                      )}{" "}
                      · approval{" "}
                      {String(
                        current.effective_security.resolved_summary
                          ?.approvalPolicy || "unknown",
                      )}{" "}
                      · reviewer{" "}
                      {String(
                        current.effective_security.resolved_summary
                          ?.approvalsReviewer || "unknown",
                      )}
                    </p>
                  </>
                ) : (
                  <p>The immutable security snapshot was not available.</p>
                )}
              </section>
              <section className="request-panel token-usage-panel">
                <h3>Token usage</h3>
                {current.token_usage ? (
                  <div className="run-facts">
                    <div>
                      <span>Total</span>
                      <strong>
                        {current.token_usage.total_tokens ?? "Not reported"}
                      </strong>
                    </div>
                    <div>
                      <span>Input</span>
                      <strong>
                        {current.token_usage.input_tokens ?? "Not reported"}
                      </strong>
                    </div>
                    <div>
                      <span>Cached input</span>
                      <strong>
                        {current.token_usage.cached_input_tokens ??
                          "Not reported"}
                      </strong>
                    </div>
                    <div>
                      <span>Cache-write input</span>
                      <strong>
                        {current.token_usage.cache_write_input_tokens ??
                          "Not reported"}
                      </strong>
                    </div>
                    <div>
                      <span>Output</span>
                      <strong>
                        {current.token_usage.output_tokens ?? "Not reported"}
                      </strong>
                    </div>
                    <div>
                      <span>Reasoning output</span>
                      <strong>
                        {current.token_usage.reasoning_output_tokens ??
                          "Not reported"}
                      </strong>
                    </div>
                  </div>
                ) : (
                  <p>Not reported</p>
                )}
              </section>
              {pending.map((item) => (
                <section className="request-panel" key={item.id}>
                  <StateBadge value="Needs review" />
                  <h3>{item.details?.title || item.kind}</h3>
                  <p>{item.details?.resource || ""}</p>
                  {item.details?.requested && (
                    <pre>{jsonText(item.details.requested)}</pre>
                  )}
                  {(item.details?.choices || []).map((choice) => (
                    <Button
                      key={choice.id}
                      variant={
                        choice.semantic === "approve" ? "default" : "outline"
                      }
                      size="sm"
                      onClick={() =>
                        onConfirm({
                          title: `Submit ${choice.label}?`,
                          description:
                            "This answers the live agent request and lets the same run continue.",
                          action: () =>
                            replyInteraction(item.id, { choiceId: choice.id }),
                        })
                      }
                    >
                      {choice.label}
                    </Button>
                  ))}
                  {item.kind === "form" && (
                    <>
                      <div className="form-answers">
                        {(item.details?.fields || [])
                          .filter((f) => f.id)
                          .map((field) => (
                            <div className="form-field" key={field.id}>
                              <Label htmlFor={`answer-${field.id}`}>
                                {field.question || field.header || field.id}
                              </Label>
                              <Input
                                id={`answer-${field.id}`}
                                value={answers[field.id] || ""}
                                onChange={(e) =>
                                  setAnswers((old) => ({
                                    ...old,
                                    [field.id]: e.target.value,
                                  }))
                                }
                              />
                              {field.options?.length ? (
                                <small>
                                  Options:{" "}
                                  {field.options.map((o) => o.label).join(", ")}
                                </small>
                              ) : null}
                            </div>
                          ))}
                      </div>
                      <Button
                        size="sm"
                        onClick={() => {
                          const fields = (item.details?.fields || []).filter(
                            (f) => f.id,
                          );
                          if (fields.some((f) => !answers[f.id]?.trim())) {
                            onNotice(
                              "Answer every question before submitting.",
                            );
                            return;
                          }
                          void replyInteraction(item.id, {
                            answers: Object.fromEntries(
                              fields.map((f) => [
                                f.id,
                                { answers: [answers[f.id].trim()] },
                              ]),
                            ),
                          });
                        }}
                      >
                        Submit answers
                      </Button>
                    </>
                  )}
                </section>
              ))}
              {!pending.length && (
                <p className="muted-note">No live requests are waiting.</p>
              )}
              <div className="inspector-actions">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() =>
                    onDetail({
                      title: "Run details",
                      description:
                        "Run state, identifiers, timestamps, and recorded result metadata.",
                      content: current,
                    })
                  }
                >
                  Run details
                </Button>
                {(current.phase === "active" || current.active) && (
                  <Button
                    variant="destructive"
                    size="sm"
                    onClick={() =>
                      onConfirm({
                        title: "Stop this run?",
                        description:
                          "The active runtime conversation is cancelled. No arbitrary process is killed.",
                        destructive: true,
                        action: async () => {
                          await api(`/api/runs/${run.run_id}/stop`, "POST");
                          onNotice("Run stopped.");
                          onClose();
                        },
                      })
                    }
                  >
                    Stop run
                  </Button>
                )}
              </div>
            </>
          )}
          {tab === "activity" &&
            (activity.length ? (
              <>
                <div className="execution-list">
                  {activity.map((item) => {
                    const details = item.details;
                    const input = objectValue(details?.input);
                    const result = objectValue(details?.result);
                    const fullTitle = String(
                      details?.title ||
                        input.summary ||
                        item.kind.replace(/_/g, " "),
                    );
                    const title =
                      fullTitle.length > 120
                        ? `${fullTitle.slice(0, 117)}…`
                        : fullTitle;
                    const activityRecord: ExecutionRecord = {
                      execution_id: item.id,
                      tool: title,
                      state: item.status,
                      started: item.created,
                      target_preview:
                        typeof input.summary === "string"
                          ? input.summary
                          : undefined,
                    };
                    const failed =
                      ["failed", "error"].includes(item.status.toLowerCase()) ||
                      result.is_error === true ||
                      result.isError === true;
                    const output = executionOutputPreview(
                      details,
                      activityRecord,
                    );
                    return (
                      <article className="execution-card" key={item.id}>
                        <header className="execution-card-head">
                          <div className="execution-tool">
                            <span className="execution-tool-icon">
                              <Command size={15} aria-hidden="true" />
                            </span>
                            <div>
                              <h3 title={fullTitle}>{title}</h3>
                              <span>
                                Activity · {item.kind.replace(/_/g, " ")}
                              </span>
                            </div>
                          </div>
                          <div className="execution-meta">
                            <ExecutionStatus
                              state={item.status}
                              isError={failed}
                            />
                            {item.created ? (
                              <time dateTime={item.created}>
                                Started {dateTime(item.created)}
                              </time>
                            ) : (
                              <span>Start time unavailable</span>
                            )}
                          </div>
                        </header>
                        <div className="execution-terminal">
                          <section className="execution-preview">
                            <h4>Input</h4>
                            <pre>
                              {executionInputPreview(activityRecord, details)}
                            </pre>
                          </section>
                          <section className="execution-preview">
                            <h4>Output preview</h4>
                            <pre>{output.text}</pre>
                          </section>
                        </div>
                        <footer className="execution-card-footer">
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() =>
                              onDetail({
                                title: `Activity ${item.id}`,
                                content: details,
                              })
                            }
                          >
                            Full record <ArrowRight size={14} />
                          </Button>
                        </footer>
                      </article>
                    );
                  })}
                </div>
                {activityCursor && (
                  <div className="feed-older" ref={loadOlderRef}>
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={loadingOlder}
                      onClick={() => void loadOlder("activity")}
                    >
                      {loadingOlder
                        ? "Loading older activities…"
                        : "Load older activities"}
                    </Button>
                  </div>
                )}
              </>
            ) : (
              <Empty title="No activities recorded" />
            ))}
          {tab === "executions" &&
            (executions.length ? (
              <div className="execution-list">
                {executions.map((ex) => {
                  const recorded = activity.find(
                    (item) => item.id === ex.execution_id,
                  );
                  const details = recorded?.details;
                  const result = objectValue(details?.result);
                  const failed = ex.is_error || result.is_error === true;
                  const output = executionOutputPreview(details, ex);
                  return (
                    <article className="execution-card" key={ex.execution_id}>
                      <header className="execution-card-head">
                        <div className="execution-tool">
                          <span className="execution-tool-icon">
                            <Command size={15} aria-hidden="true" />
                          </span>
                          <div>
                            <h3>{ex.tool || "Tool"}</h3>
                            <span>Execution #{ex.sequence || "?"}</span>
                          </div>
                        </div>
                        <div className="execution-meta">
                          <ExecutionStatus state={ex.state} isError={failed} />
                          {ex.started ? (
                            <time dateTime={ex.started}>
                              Started {dateTime(ex.started)}
                            </time>
                          ) : (
                            <span>Start time unavailable</span>
                          )}
                        </div>
                      </header>
                      <div className="execution-terminal">
                        <section className="execution-preview">
                          <h4>Input</h4>
                          <pre>{executionInputPreview(ex, details)}</pre>
                        </section>
                        <section className="execution-preview">
                          <h4>Output preview</h4>
                          <pre>{output.text}</pre>
                        </section>
                      </div>
                      <footer className="execution-card-footer">
                        {typeof ex.duration_ms === "number" && (
                          <span>{(ex.duration_ms / 1000).toFixed(1)}s</span>
                        )}
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={async () => {
                            try {
                              const detail = await api(
                                `/api/runs/${run.run_id}/executions/${encodeURIComponent(ex.execution_id)}`,
                              );
                              onDetail({
                                title: `Execution ${ex.execution_id}`,
                                content: detail,
                              });
                            } catch (error) {
                              onNotice((error as Error).message);
                            }
                          }}
                        >
                          Full record <ArrowRight size={14} />
                        </Button>
                      </footer>
                    </article>
                  );
                })}
                {executionCursor && (
                  <div className="feed-older" ref={loadOlderRef}>
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={loadingOlder}
                      onClick={() => void loadOlder("executions")}
                    >
                      {loadingOlder
                        ? "Loading older executions…"
                        : "Load older executions"}
                    </Button>
                  </div>
                )}
              </div>
            ) : (
              <Empty title="No executions recorded" />
            ))}
        </div>
      </SheetContent>
    </Sheet>
  );
}
