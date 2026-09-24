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
  Settings2,
  Shield,
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
  type Event,
  type Handoff,
  type Json,
  type Model,
  type ModelPolicy,
  type Run,
  type Status,
  type Workspace,
} from "@/lib/api";
import "./app.css";
import { ProfileManager } from "./ProfileEditor";
import { ProfileAssignment } from "./ProfileAssignment";

type Section =
  | "overview"
  | "workspaces"
  | "profiles"
  | "handoffs"
  | "runs"
  | "runtimes"
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
    id: "workspaces",
    title: "Workspaces",
    icon: FolderClosed,
    description: "Access to your projects",
  },
  {
    id: "profiles",
    title: "Profiles",
    icon: Shield,
    description: "Security controls for each runtime",
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
    id: "runtimes",
    title: "Runtimes",
    icon: Command,
    description: "Adapters, models, and connection",
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
  return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}
function StateBadge({ value }: { value: string }) {
  const lower = value.toLowerCase();
  const tone = /healthy|ready|completed|succeeded|enabled|active/.test(lower)
    ? "success"
    : /waiting|review|pending|paused/.test(lower)
      ? "warning"
      : /failed|error|unavailable|locked/.test(lower)
        ? "danger"
        : "neutral";
  return (
    <Badge variant="outline" className={`state-badge tone-${tone}`}>
      {value}
    </Badge>
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
          <span>{runtimeName(run.runtime || "unknown")}</span>
        </div>
        <h3>{run.handoff_title || run.job_id || "Untitled handoff"}</h3>
        <p>
          {run.workspace_name || run.workspace_id || "Workspace"}{" "}
          <span aria-hidden="true">/</span> {run.model || "Default model"}
        </p>
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
  onManage,
  onProfile,
  onHandoffs,
  onConfirm,
  onNotice,
  onDetail,
}: {
  ws: Workspace;
  onManage: (ws: Workspace, operation: string, extra?: Json) => Promise<void>;
  onProfile: (ws: Workspace, runtimeId: string) => void;
  onHandoffs: (ws: Workspace) => void;
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
          <p title={ws.root}>{ws.root}</p>
        </div>
        <Button variant="outline" size="sm" onClick={() => onHandoffs(ws)}>
          Handoffs <ArrowRight size={14} />
        </Button>
      </div>
      <div className="access-matrix">
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
        <div className="access-cell">
          <div>
            <strong>Agent runs</strong>
            <p>Prepared handoffs can start an agent.</p>
          </div>
          <Switch
            aria-label={`Agent runs for ${ws.name}`}
            checked={ws.agent_enabled}
            onCheckedChange={(enabled) =>
              change(
                enabled ? "Enable agent runs?" : "Disable agent runs?",
                enabled
                  ? "An authorized chat can start bounded agent runs here. The native runtime can read and write under its security profile."
                  : "Existing runs continue until stopped.",
                () =>
                  onManage(ws, "set_agent_enabled", { agent_enabled: enabled }),
                !enabled,
              )
            }
          />
        </div>
        {Object.entries(ws.runtime_grants || {}).map(([id, grant]) => (
          <div className="access-cell runtime-access" key={id}>
            <div>
              <strong>{runtimeName(id)} runtime</strong>
              <p>
                {grant.security_binding?.source === "runtime-config"
                  ? "Use Codex config (config.toml)"
                  : grant.profile
                    ? `Profile: ${grant.profile.id}`
                    : "Choose a security profile"}
              </p>
              {grant.security_binding?.source === "runtime-config" && (
                <p className="runtime-security-summary">
                  {grant.security_binding.status === "ready"
                    ? `Following current Codex config · ${grant.security_binding.resolved_summary?.activePermissionProfile || "Codex default"} · ${grant.security_binding.resolved_summary?.approvalPolicy || "approval unknown"} · ${grant.security_binding.resolved_summary?.approvalsReviewer || "reviewer unknown"}`
                    : "Codex security config is currently unavailable"}
                </p>
              )}
              <Button
                variant="link"
                size="sm"
                className="inline-action"
                onClick={() => onProfile(ws, id)}
              >
                Change security
              </Button>
            </div>
            <Switch
              aria-label={`${runtimeName(id)} runtime for ${ws.name}`}
              checked={grant.enabled}
              onCheckedChange={(enabled) =>
                change(
                  enabled
                    ? `Allow ${runtimeName(id)} here?`
                    : `Revoke ${runtimeName(id)} here?`,
                  enabled
                    ? "The workspace agent switch and model policy must also be enabled."
                    : "Active runs are not stopped automatically.",
                  () =>
                    api(`/api/workspaces/${ws.id}/runtimes/${id}`, "POST", {
                      enabled,
                    }).then(() =>
                      onNotice(
                        `${runtimeName(id)} ${enabled ? "allowed" : "revoked"} for ${ws.name}.`,
                      ),
                    ),
                  !enabled,
                )
              }
            />
          </div>
        ))}
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
  runtime,
  open,
  onClose,
  workspaces,
  policy,
  onSaved,
  onNotice,
}: {
  runtime: string | null;
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
      if (!runtime || !selected) return;
      setLoading(true);
      try {
        const data = await api<{ models: Model[]; policy?: ModelPolicy }>(
          `/api/runtimes/${runtime}/models?limit=100&workspace_id=${encodeURIComponent(selected)}`,
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
    [runtime, onNotice],
  );
  useEffect(() => {
    if (open && workspaceId) void load(workspaceId);
  }, [open, workspaceId, load]);
  const visible = models.filter((m) =>
    `${m.selector} ${m.displayName || m.name || ""}`
      .toLowerCase()
      .includes(filter.toLowerCase()),
  );
  async function save() {
    if (
      !runtime ||
      !workspaceId ||
      !enabled.length ||
      !defaultModel ||
      !enabled.includes(defaultModel)
    ) {
      onNotice("Enable a model and choose its default.");
      return;
    }
    try {
      await api(`/api/runtimes/${runtime}/model-policy`, "POST", {
        workspace_id: workspaceId,
        enabled,
        default: defaultModel,
        reasoning_defaults: reasoningDefaults,
      });
      onNotice(`${runtimeName(runtime)} model policy saved.`);
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
          <DialogTitle>{runtimeName(runtime || "")} models</DialogTitle>
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
                    ? `Use runtime default (${thinkingEffortLabel(m.defaultReasoningEffort)})`
                    : "Use runtime default";
                  return (
                    <div
                      className={
                        runtime === "codex"
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
  const [auth, setAuth] = useState<"checking" | "login" | "ready">("checking");
  const [loginError, setLoginError] = useState("");
  const [theme, setTheme] = useState(initialTheme);
  const [section, setSection] = useState<Section>(initialSection);
  const [mobileNav, setMobileNav] = useState(false);
  const [status, setStatus] = useState<Status | null>(null);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [events, setEvents] = useState<Event[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [runsNext, setRunsNext] = useState<number | null>(null);
  const [selectedWorkspace, setSelectedWorkspace] = useState<string | null>(
    null,
  );
  const [handoffs, setHandoffs] = useState<Handoff[]>([]);
  const [workspaceRuns, setWorkspaceRuns] = useState<Run[]>([]);
  const [workspaceQuery, setWorkspaceQuery] = useState("");
  const [addOpen, setAddOpen] = useState(false);
  const [profileFor, setProfileFor] = useState<{
    ws: Workspace;
    runtime: string;
  } | null>(null);
  const [modelRuntime, setModelRuntime] = useState<string | null>(null);
  const [runInspect, setRunInspect] = useState<Run | null>(null);
  const [textDetail, setTextDetail] = useState<TextDetail | null>(null);
  const [confirm, setConfirm] = useState<ConfirmState | null>(null);
  const [notice, setNotice] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const refreshingRef = useRef(false);
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
        const [workspaceData, historyData, currentStatus] = await Promise.all([
          api<{ workspaces: Workspace[] }>("/api/workspaces"),
          api<{ events: Event[] }>("/api/events"),
          api<Status>("/api/status"),
        ]);
        setWorkspaces(workspaceData.workspaces || []);
        setEvents(historyData.events || []);
        setStatus(currentStatus);
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
        await api<Status>("/api/status");
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
    const token = ((new FormData(form).get("token") as string) || "").trim();
    form.reset();
    try {
      const response = await fetch("/api/login", {
        method: "POST",
        credentials: "same-origin",
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok)
        throw new Error(
          response.status === 401
            ? "That token is not current. Use the token from the active production state directory."
            : `Sign in failed (HTTP ${response.status}).`,
        );
      await refresh();
      setAuth("ready");
    } catch (error) {
      setLoginError((error as Error).message);
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
    setStatus(null);
    setWorkspaces([]);
    setEvents([]);
    setRuns([]);
    setHandoffs([]);
    setRunInspect(null);
    setTextDetail(null);
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
    navigate("handoffs");
    try {
      await loadWorkspaceDetails(ws.id);
    } catch (error) {
      notify((error as Error).message);
    }
  }
  function openProfile(ws: Workspace, runtime: string) {
    setProfileFor({ ws, runtime });
  }
  async function addWorkspace(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    try {
      const result = await api<{ note?: string }>("/api/workspaces", "POST", {
        name: data.get("name"),
        root: data.get("root"),
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
  const adapters = status?.runtimes?.runtimes || {};
  const policies = useMemo(() => status?.runtime_policies || {}, [status]);
  const activeRuns = runs.filter((r) => r.phase === "active" || r.active);
  const attentionRuns = runs.filter(
    (r) => r.active_state === "waiting_interaction",
  );
  const setup = useMemo(
    () => [
      {
        title: "Add a workspace",
        done: workspaces.some((w) => w.enabled),
        target: "workspaces" as Section,
        text: "Choose a project and enable bridge access.",
      },
      {
        title: "Connect the bridge",
        done: Boolean(status?.bridge.enabled),
        target: "runtimes" as Section,
        text: "Create a credential for the shared connection.",
      },
      {
        title: "Grant agent access",
        done: workspaces.some(
          (w) =>
            w.enabled &&
            w.agent_enabled &&
            Object.values(w.runtime_grants || {}).some(
              (g) => g.enabled && g.profile,
            ),
        ),
        target: "workspaces" as Section,
        text: "Allow runs and choose a security profile.",
      },
      {
        title: "Choose a model",
        done: Object.values(policies).some((p) => p.configured),
        target: "runtimes" as Section,
        text: "Enable a model and set its default.",
      },
    ],
    [workspaces, status, policies],
  );
  const ready = setup.every((s) => s.done);
  const page = sections.find((s) => s.id === section)!;
  if (auth === "checking")
    return (
      <div className="loading-shell">
        <div className="bridge-mark">
          <Workflow size={27} />
        </div>
        <Skeleton className="h-7 w-52" />
        <Skeleton className="h-4 w-72" />
        <span className="sr-only">Checking session</span>
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
            <div className="signal-line">
              <i />
              <i />
              <i />
            </div>
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
            <span className="section-kicker">Local manager</span>
            <h2>Open the console</h2>
            <p>Enter the current admin token to manage this Bridge.</p>
            <form onSubmit={login}>
              <Label htmlFor="admin-token">Admin token</Label>
              <Input
                id="admin-token"
                name="token"
                type="password"
                autoComplete="off"
                placeholder="Paste admin token"
                required
              />
              <Button type="submit" size="lg">
                Open manager <ArrowRight size={16} />
              </Button>
            </form>
            {loginError && (
              <p className="form-error" role="alert">
                {loginError}
              </p>
            )}
            <small>
              A database reset creates a new token in the private state
              directory.
            </small>
          </div>
        </main>
        <footer className="login-foot">Local agent operations</footer>
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
          {sections.map((s) => (
            <button
              type="button"
              key={s.id}
              className={`nav-item ${section === s.id ? "current" : ""}`}
              aria-current={section === s.id ? "page" : undefined}
              onClick={() => navigate(s.id)}
            >
              <s.icon size={17} />
              <span>{s.title}</span>
              {s.id === "runs" && attentionRuns.length > 0 && (
                <b>{attentionRuns.length}</b>
              )}
            </button>
          ))}
        </nav>
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
            <span className="breadcrumb">Workspace Bridge</span>
            <span className="breadcrumb-sep">/</span>
            <strong>{page.title}</strong>
          </div>
          <div className="topbar-right">
            <span className="topbar-health">
              <span className="live-dot" />
              {status?.bridge.enabled ? "Bridge online" : "Bridge setup needed"}
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
              <h1>{page.title}</h1>
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
                  Updated {new Date(lastUpdated).toLocaleTimeString()}
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
              <div
                className={`mission-banner ${attentionRuns.length ? "mission-attention" : ready ? "mission-ready" : ""}`}
              >
                <div className="mission-icon">
                  {attentionRuns.length ? (
                    <CircleHelp />
                  ) : ready ? (
                    <Check />
                  ) : (
                    <Workflow />
                  )}
                </div>
                <div>
                  <span className="section-kicker">Current position</span>
                  <h2>
                    {attentionRuns.length
                      ? `${attentionRuns.length} run${attentionRuns.length === 1 ? " needs" : "s need"} review`
                      : activeRuns.length
                        ? `${activeRuns.length} run${activeRuns.length === 1 ? "" : "s"} in progress`
                        : ready
                          ? "Ready for the next handoff"
                          : "Bring a workspace online"}
                  </h2>
                  <p>
                    {attentionRuns.length
                      ? "An agent is waiting for a decision. Open the run to continue."
                      : ready
                        ? "Workspace access, the bridge, runtime grants, and models are configured."
                        : "Follow the access path below to get this Bridge ready."}
                  </p>
                </div>
                <Button
                  onClick={() =>
                    navigate(
                      attentionRuns.length
                        ? "runs"
                        : ready
                          ? "handoffs"
                          : setup.find((s) => !s.done)?.target || "workspaces",
                    )
                  }
                >
                  {attentionRuns.length
                    ? "Review runs"
                    : ready
                      ? "Open handoffs"
                      : "Continue setup"}
                  <ArrowRight size={15} />
                </Button>
              </div>
              <div className="overview-columns">
                <section className="surface-panel">
                  <SectionHeading
                    title="Access path"
                    description={`${setup.filter((s) => s.done).length} of 4 steps ready`}
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
                          {step.done ? <Check size={15} /> : index + 1}
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
          {section === "workspaces" && (
            <div className="workspaces-page">
              <div className="list-toolbar">
                <div className="search-box">
                  <Search size={17} />
                  <Input
                    aria-label="Find a workspace"
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
                      onManage={manage}
                      onProfile={(w, runtime) => void openProfile(w, runtime)}
                      onHandoffs={(w) => void openHandoffs(w)}
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
          {section === "profiles" && (
            <ProfileManager
              runtimes={Object.entries(adapters)
                .filter(([, info]) => info.configured)
                .map(([id]) => id)}
              workspaces={workspaces}
              onChanged={refresh}
              notify={notify}
            />
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
                                    const data = await api(
                                      `/api/workspaces/${selected.id}/document?${new URLSearchParams({ job_id: h.id, document: doc })}`,
                                    );
                                    setTextDetail({
                                      title: `${h.title} — ${doc}`,
                                      content: data,
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
                        Enable agent runs and start a prepared handoff.
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
          {section === "runtimes" && (
            <div className="runtimes-page">
              <SectionHeading
                title="Adapters"
                description="Each runtime owns its native engine. Bridge decides where it may run."
              />
              <div className="adapter-list">
                {Object.entries(adapters).map(([id, info]) => {
                  const policy = policies[id];
                  const healthy = info.configured && info.healthy;
                  return (
                    <section className="adapter-row" key={id}>
                      <div className="adapter-symbol">
                        <Command size={21} />
                      </div>
                      <div className="adapter-info">
                        <div className="adapter-heading">
                          <h3>{runtimeName(id)}</h3>
                          <StateBadge
                            value={
                              !info.configured
                                ? "Not configured"
                                : info.locked
                                  ? "Locked"
                                  : healthy
                                    ? "Healthy"
                                    : "Unavailable"
                            }
                          />
                        </div>
                        <p>
                          {info.protocol === 1
                            ? "Runtime Protocol v1 adapter"
                            : "Native runtime adapter"}
                          {info.native_version || info.version
                            ? ` · ${info.native_version || info.version}`
                            : ""}
                        </p>
                        <div className="adapter-policy">
                          {policy?.configured ? (
                            <>
                              <span>
                                {policy.enabled_count ??
                                  policy.enabled?.length ??
                                  0}{" "}
                                models enabled
                              </span>
                              <span>Default: {policy.default}</span>
                            </>
                          ) : (
                            <span>Model policy not configured</span>
                          )}
                        </div>
                      </div>
                      <div className="adapter-actions">
                        <Button
                          variant="outline"
                          onClick={() => setModelRuntime(id)}
                        >
                          Manage models <ArrowRight size={14} />
                        </Button>
                      </div>
                    </section>
                  );
                })}
                {!Object.keys(adapters).length && (
                  <Empty title="No adapters connected">
                    Configure a runtime adapter to run prepared handoffs.
                  </Empty>
                )}
              </div>
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
                        ? "Not configured"
                        : status.bridge.enabled
                          ? "Enabled"
                          : "Paused"
                    }
                  />
                </div>
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
                        <strong>{event.action}</strong>
                        <p>
                          {workspaces.find((w) => w.id === event.workspace)
                            ?.name || "Admin"}{" "}
                          <span aria-hidden="true">/</span> {event.outcome}
                        </p>
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
      </div>
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
              <Label htmlFor="add-root">Project directory</Label>
              <Input
                id="add-root"
                name="root"
                placeholder="/Users/you/Projects/my-project"
                required
              />
              <small>
                Allowed parent: {status?.allowed_parents?.join(", ") || "—"}
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
      {profileFor && (
        <ProfileAssignment
          workspace={profileFor.ws}
          runtime={profileFor.runtime}
          onClose={() => setProfileFor(null)}
          onManage={() => {
            setProfileFor(null);
            navigate("profiles");
          }}
          onChanged={refresh}
          notify={notify}
        />
      )}
      {modelRuntime && (
        <ModelDialog
          runtime={modelRuntime}
          open
          onClose={() => setModelRuntime(null)}
          workspaces={workspaces}
          policy={policies[modelRuntime]}
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
      <span className="bridge-mark">
        <Workflow size={21} strokeWidth={2.3} />
      </span>
      <span>
        <strong>Workspace Bridge</strong>
        <small>Local agent operations</small>
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
            <span>{runtimeName(current.runtime || "unknown")}</span>
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
              </div>
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
                    const title = String(
                      details?.title ||
                        input.summary ||
                        item.kind.replace(/_/g, " "),
                    );
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
                              <h3>{title}</h3>
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
