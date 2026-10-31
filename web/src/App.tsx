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
import type { ConfirmState, TextDetail } from "@/components/manager-types";
import { RunInspector } from "@/features/runs/RunInspector";
import { WorkspaceCheckpoints } from "@/features/runs/WorkspaceCheckpoints";
import { getRuns, getWorkspaceRuns } from "@/features/runs/api";
import { useManagerEvents } from "@/features/runs/useManagerEvents";
import { createRefreshQueue } from "@/features/runs/refresh-queue";
import {
  appendOlder,
  mergeNewest,
  newestPageHasGap,
} from "@/features/runs/feed-state";
import "./app.css";
import { ProfileManager } from "./ProfileEditor";
import { ProfileAssignment } from "./ProfileAssignment";
import { ApprovalLog } from "./ApprovalLog";
import { StartRunDialog } from "./StartRunDialog";
import {
  AdapterDialog,
  NodeDialog,
  type AdapterDialogState,
  type NodeDialogState,
} from "./SetupDialogs";
import { CommandPalette, type PaletteCommand } from "./CommandPalette";

type Section =
  | "overview"
  | "nodes"
  | "workspaces"
  | "adapters"
  | "handoffs"
  | "runs"
  | "audit";
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
    id: "audit",
    title: "Audit",
    icon: Shield,
    description: "Access and configuration history",
  },
];
const pageSize = 25;
// Codex account quota is read only while the Adapters section is visible and
// refreshed at most once per adapter per minute; never from /api/status.
const quotaRefreshMs = 60000;
const AUDIT_ACTION_LABELS: Record<string, string> = {
  node_paired: "Node paired",
  node_pairing_created: "Node pairing created",
  bridge_rotate_token: "Bridge token rotated",
  bridge_enable: "Bridge access enabled",
  bridge_disable: "Bridge access paused",
  workspace_registered: "Workspace registered",
  set_workspace_route: "Workspace route changed",
  abandon_agent_run: "Agent run abandoned",
  internal_error: "Internal error",
  admin_internal_error: "Internal error",
};

/** Event codes are snake_case identifiers; show words, keep the code as a tooltip. */
function auditActionLabel(action: string): string {
  const known = AUDIT_ACTION_LABELS[action];
  if (known) return known;
  const words = action.replace(/[_.:]+/g, " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : action;
}

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
  if (effective.error) return `Unavailable — ${effective.error}`;
  if (effective.source === "profile") {
    return `Profile ${effective.profile_id || "unknown"} · revision ${(effective.revision || "unknown").slice(0, 17)}`;
  }
  return "The agent's own security; each approval it asks for becomes an interaction.";
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
                {!diagnosticsUnavailable && route && !route.ready && (
                  <div className="runtime-route-blocker">
                    <p>{blockers[0]?.check?.summary || route.summary}</p>
                    {blockers[0]?.check?.remediation && (
                      <small>{blockers[0].check.remediation}</small>
                    )}
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
                <span className="route-switch-label" aria-hidden="true">
                  {grant.enabled ? "Route on" : "Route off"}
                </span>
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
            {ws.write_scope === "workspace" ? "Edits allowed" : "Read only"}
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
            <Label htmlFor={`write-${ws.id}`}>ChatGPT file access</Label>
            <p id={`write-help-${ws.id}`}>
              {writeScope === "workspace"
                ? "ChatGPT can edit files that are not excluded and run the workspace's checks. Agent runs are governed separately by each route's security profile."
                : "ChatGPT can list and read files but cannot change anything. Agent runs are governed separately by each route's security profile."}
            </p>
            <select
              id={`write-${ws.id}`}
              aria-describedby={`write-help-${ws.id}`}
              className="native-select"
              value={writeScope}
              onChange={(e) =>
                setWriteScope(e.target.value as Workspace["write_scope"])
              }
            >
              <option value="none">Read only</option>
              <option value="workspace">
                Edit permitted files and run checks
              </option>
            </select>
          </div>
          <div className="settings-field">
            <Label htmlFor={`excludes-${ws.id}`}>Extra exclusions</Label>
            <p>
              Built-in exclusions always apply. One pattern per line. A line
              such as <code>!build</code> or <code>!vendor</code> shows a folder
              hidden by default; credential and Git paths stay hidden.
            </p>
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
          <WorkspaceCheckpoints
            workspaceId={ws.id}
            onConfirm={onConfirm}
            onNotice={onNotice}
          />
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
  const eligible = adapter?.node_id
    ? workspaces.filter((w) => w.enabled && w.node_id === adapter.node_id)
    : [];
  const [selectedWorkspaceId, setWorkspaceId] = useState("");
  const workspaceId =
    eligible.find((w) => w.id === selectedWorkspaceId)?.id ||
    eligible[0]?.id ||
    "";
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
          <Empty title="Enable a workspace on this Node">
            Model discovery needs an enabled workspace on{" "}
            {adapter?.node_name || "the adapter’s Node"}.
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
                        adapter?.runtime_type === "claude" ||
                        adapter?.runtime_type === "opencode"
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
  const [adapterDialog, setAdapterDialog] = useState<AdapterDialogState | null>(
    null,
  );
  const [nodeDialog, setNodeDialog] = useState<NodeDialogState | null>(null);
  const [startRun, setStartRun] = useState(false);
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
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const refreshingRef = useRef(false);
  const runsRequestRef = useRef(0);
  const runsDataRef = useRef<Run[]>([]);
  const runsOlderLoadedRef = useRef(false);
  const selectedWorkspaceRef = useRef(selectedWorkspace);
  useEffect(() => {
    selectedWorkspaceRef.current = selectedWorkspace;
  }, [selectedWorkspace]);
  const liveRefreshRef = useRef<ReturnType<typeof createRefreshQueue> | null>(
    null,
  );
  const snapshotRefreshRef = useRef<ReturnType<
    typeof createRefreshQueue
  > | null>(null);
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
  const loadRuns = useCallback(
    async (
      offset = 0,
      options?: { preservePages?: boolean; signal?: AbortSignal },
    ) => {
      const request = offset
        ? runsRequestRef.current
        : ++runsRequestRef.current;
      const data = await getRuns(offset, pageSize, options?.signal);
      if (options?.signal?.aborted || request !== runsRequestRef.current)
        return data;
      const previous = runsDataRef.current;
      const resetPage =
        !offset &&
        (!options?.preservePages ||
          newestPageHasGap(
            data.runs,
            previous,
            (item) => item.run_id,
            data.next_offset !== null,
          ));
      const next = offset
        ? appendOlder(previous, data.runs, (item) => item.run_id)
        : resetPage
          ? data.runs
          : mergeNewest(data.runs, previous, (item) => item.run_id);
      runsDataRef.current = next;
      setRuns(next);
      if (offset) runsOlderLoadedRef.current = true;
      else if (resetPage) runsOlderLoadedRef.current = false;
      if (offset || !options?.preservePages || !runsOlderLoadedRef.current)
        setRunsNext(data.next_offset);
      return data;
    },
    [],
  );
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
        const [workspaceData, historyData, currentStatus] = await Promise.all([
          api<{ workspaces: Workspace[] }>("/api/workspaces"),
          api<{ events: Event[] }>("/api/events"),
          api<Status>("/api/status"),
          diagnosticsResult,
        ]);
        setWorkspaces(workspaceData.workspaces || []);
        setEvents(historyData.events || []);
        setStatus(currentStatus);
        setNodes(currentStatus.nodes || []);
        try {
          await loadRuns(0, { preservePages: true });
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
      getWorkspaceRuns(workspaceId),
    ]);
    if (selectedWorkspaceRef.current !== workspaceId) return;
    setHandoffs(a.handoffs || []);
    setWorkspaceRuns(b.runs || []);
  }, []);
  useEffect(() => {
    if (auth !== "ready" || !autoRefresh) return;
    let active = true;
    const controller = new AbortController();
    const queue = createRefreshQueue(async () => {
      if (document.hidden) return;
      await Promise.allSettled([
        loadRuns(0, { preservePages: true, signal: controller.signal }),
        section === "handoffs" && selectedWorkspace
          ? getWorkspaceRuns(selectedWorkspace, controller.signal).then(
              (page) => {
                if (
                  active &&
                  selectedWorkspaceRef.current === selectedWorkspace
                )
                  setWorkspaceRuns(page.runs || []);
              },
            )
          : Promise.resolve(),
      ]);
    });
    liveRefreshRef.current = queue;
    return () => {
      active = false;
      controller.abort();
      queue.dispose();
      if (liveRefreshRef.current === queue) liveRefreshRef.current = null;
    };
  }, [auth, autoRefresh, loadRuns, section, selectedWorkspace]);
  const refreshLiveRuns = useCallback(() => {
    if (!document.hidden) liveRefreshRef.current?.schedule();
  }, []);
  const refreshSnapshot = useCallback(() => {
    if (!document.hidden) snapshotRefreshRef.current?.schedule();
  }, []);
  useManagerEvents({
    enabled: auth === "ready" && autoRefresh,
    onRun: refreshLiveRuns,
    onState: refreshSnapshot,
    onReset: () => {
      refreshLiveRuns();
      refreshSnapshot();
    },
  });
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
    // The Bridge pushes a hint when audited state moves; reread the whole
    // snapshot once per burst, and again when the tab becomes visible.
    const queue = createRefreshQueue(async () => {
      if (document.hidden || refreshingRef.current) return;
      try {
        await refresh({ silent: true });
        if (section === "handoffs" && selectedWorkspace)
          await loadWorkspaceDetails(selectedWorkspace);
      } catch {
        /* keep stale page data until the next hint */
      }
    });
    snapshotRefreshRef.current = queue;
    const onVisible = () => {
      if (!document.hidden) queue.schedule();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      document.removeEventListener("visibilitychange", onVisible);
      queue.dispose();
      if (snapshotRefreshRef.current === queue)
        snapshotRefreshRef.current = null;
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
    runsRequestRef.current++;
    runsDataRef.current = [];
    runsOlderLoadedRef.current = false;
    setRuns([]);
    setRunsNext(null);
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
  const onlyWorkspace = workspaces.length === 1 ? workspaces[0] : null;
  useEffect(() => {
    // With a single workspace there is nothing to choose: open its handoffs.
    if (section === "handoffs" && !selectedWorkspace && onlyWorkspace)
      void openHandoffs(onlyWorkspace);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [section, selectedWorkspace, onlyWorkspace?.id]);
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
  useEffect(() => {
    document.title = `${page.title} · Workspace Bridge`;
  }, [page.title]);
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
        id: "pair-node",
        group: "Actions",
        label: "Pair a Node",
        run: () => {
          navigate("nodes");
          setNodeDialog({ mode: "pair" });
        },
      },
      {
        id: "add-adapter",
        group: "Actions",
        label: "Add an adapter",
        run: () => {
          navigate("adapters");
          setAdapterDialog({ mode: "create" });
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
        label: autoRefresh ? "Pause live updates" : "Resume live updates",
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
              className="topbar-password"
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
                  aria-label="Update data live"
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
                <div className="diagnostic-observation-list">
                  <p>
                    Bridge gateway:{" "}
                    {status?.bridge.configured
                      ? "configured"
                      : "not configured"}{" "}
                    · {status?.bridge.enabled ? "enabled" : "disabled"}
                  </p>
                  <p>Bridge version {status?.version || "unknown"}</p>
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
                      <br />
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => navigate("handoffs")}
                      >
                        Open Handoffs
                      </Button>
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
                    setNodeDialog({ mode: "pair" });
                  }}
                >
                  <Plus size={16} /> Pair a Node
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
                              <p className="path-text">
                                {node.transport === "local"
                                  ? "Embedded in the Bridge"
                                  : "Paired · dials the Bridge"}
                              </p>
                            </div>
                          </div>
                          <div className="row-actions">
                            <Button
                              variant="outline"
                              onClick={() => {
                                setNodeDialog({ mode: "edit", node });
                              }}
                            >
                              Edit
                            </Button>
                            {node.transport !== "local" && (
                              <Button
                                variant="outline"
                                onClick={() =>
                                  ask({
                                    title: `Delete ${node.name}?`,
                                    description:
                                      "Deletion is available only when no workspace or adapter history references this Node. A paired Node is disconnected and must be paired again to come back.",
                                    destructive: true,
                                    action: async () => {
                                      await api(
                                        `/api/nodes/${node.id}`,
                                        "DELETE",
                                      );
                                      await refresh();
                                      notify("Node deleted.");
                                    },
                                  })
                                }
                              >
                                Delete
                              </Button>
                            )}
                            <Button
                              onClick={() => {
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
                              <dt>Connection</dt>
                              <dd>
                                {node.transport === "local"
                                  ? "In process"
                                  : node.connection?.connected_at
                                    ? `Last connected ${dateTime(node.connection.connected_at)}`
                                    : "Never connected"}
                              </dd>
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
                  title={selected?.name}
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
                            ? "Route readiness is unavailable until diagnostics refresh."
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
                  <>
                    <span className="subtle-count">{runs.length} shown</span>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => setStartRun(true)}
                    >
                      Start run by hand
                    </Button>
                  </>
                }
              />
              {runs.length ? (
                runs.map((run) => (
                  <RunCard key={run.run_id} run={run} onOpen={setRunInspect} />
                ))
              ) : (
                <Empty title="No runs yet">
                  Start an agent from a prepared handoff to see its progress
                  here. A workspace needs an enabled execution target first.
                  <br />
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => navigate("handoffs")}
                  >
                    Open Handoffs
                  </Button>
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
                    setAdapterDialog({ mode: "create" });
                  }}
                >
                  <Plus size={16} /> Add adapter
                </Button>
                <nav className="page-jumps" aria-label="On this page">
                  {[
                    ["adapter-profiles", "Security profiles"],
                    ["adapter-connection", "Bridge connection"],
                  ].map(([id, label]) => (
                    <Button
                      key={id}
                      variant="ghost"
                      size="sm"
                      onClick={() =>
                        document
                          .getElementById(id)
                          ?.scrollIntoView({ behavior: "smooth" })
                      }
                    >
                      {label}
                    </Button>
                  ))}
                </nav>
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
                                {info.name.trim().toLowerCase() ===
                                runtimeName(info.runtime_type).toLowerCase()
                                  ? `${info.name} on ${info.node_name || "an unknown Node"}`
                                  : `${info.name} · ${runtimeName(info.runtime_type)}`}
                              </h3>
                              <StateBadge value={health} />
                            </div>
                            <p className="path-text">
                              Runs on {info.node_name || "an unknown Node"}
                            </p>
                          </div>
                        </div>
                        <div className="adapter-actions">
                          <Button
                            variant="outline"
                            onClick={() => {
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
                            <dt>Adapter version</dt>
                            <dd>{info.adapter_version || "Unknown"}</dd>
                          </div>
                          <div>
                            <dt>Native version</dt>
                            <dd>{info.native_version || "Not observed"}</dd>
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
                    Add Local Pi, GPU Pi, Codex, Claude Code, or OpenCode as
                    separate execution targets.
                  </Empty>
                )}
              </div>
              <section
                id="adapter-profiles"
                className="surface-panel adapter-detail-panel"
              >
                <SectionHeading
                  title="Security profiles"
                  description="One format for every agent, assigned per workspace route."
                />
                <ProfileManager onChanged={refresh} notify={notify} />
              </section>
              <div id="adapter-connection" className="connection-panel">
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
                        <strong title={event.action}>
                          {auditActionLabel(event.action)}
                        </strong>
                        <p>
                          {event.workspace
                            ? workspaces.find((w) => w.id === event.workspace)
                                ?.name || "Removed workspace"
                            : "Bridge"}{" "}
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
              <ApprovalLog refreshKey={lastUpdated} />
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
          <span>live {autoRefresh ? "on" : "off"}</span>
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
            <button
              type="button"
              className="nav-item"
              onClick={() => {
                setMobileNav(false);
                setLoginError("");
                setChangingPassword(true);
              }}
            >
              <LockKeyhole size={17} />
              Change password
            </button>
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
      {startRun && (
        <StartRunDialog
          workspaces={workspaces}
          onClose={() => setStartRun(false)}
          onStarted={(run) => {
            setStartRun(false);
            notify("Run started.");
            void refresh({ silent: true });
            setRunInspect(run);
          }}
        />
      )}
      {nodeDialog && (
        <NodeDialog
          dialog={nodeDialog}
          onClose={() => setNodeDialog(null)}
          onDone={(message) => {
            setNodeDialog(null);
            notify(message);
            void refresh();
          }}
        />
      )}
      {adapterDialog && (
        <AdapterDialog
          dialog={adapterDialog}
          nodes={nodes}
          onClose={() => setAdapterDialog(null)}
          onDone={(message) => {
            setAdapterDialog(null);
            notify(message);
            void refresh();
          }}
        />
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
          key={modelAdapterId}
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
          lastMcpCall={status?.bridge.last_mcp_call}
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
          <div className="detail-actions">
            <Button
              variant="outline"
              size="sm"
              disabled={!textDetail}
              onClick={async () => {
                try {
                  await navigator.clipboard.writeText(
                    textDetail ? jsonText(textDetail.content) : "",
                  );
                  notify("Copied.");
                } catch {
                  notify("Copy is unavailable here; select the text instead.");
                }
              }}
            >
              <Clipboard size={14} /> Copy
            </Button>
          </div>
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
