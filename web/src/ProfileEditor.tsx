import { useCallback, useEffect, useState } from "react";
import { api, runtimeName, type AdapterInfo, type Workspace } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
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

type Mode = "choose" | "create" | "edit";
type AccessMode = "allow" | "ask" | "deny";
type Profile = {
  id: string;
  revision: string;
  definitionRevision?: string;
  mutable: boolean;
  available?: boolean;
  config: Record<string, unknown> | null;
};
type NativePermissionProfile = {
  id: string;
  description: string;
  allowed: boolean;
};
type PiConfig = {
  version: number;
  write_tools_enabled: boolean;
  tools: Record<"read" | "grep" | "find" | "ls" | "edit" | "write", AccessMode>;
  protected_patterns: string[];
  protected_template_exceptions: string[];
  allow_session_always: boolean;
  external_access: {
    default_mode: AccessMode;
    roots: Array<{ path: string; mode: AccessMode }>;
  };
  shell_mode: AccessMode;
};
type CodexConfig = {
  permissions: string;
  approvalPolicy: "on-request" | "never";
  approvalsReviewer: "user" | "auto_review";
};
type ClaudeConfig = {
  edits: AccessMode;
  shell: AccessMode;
  web: AccessMode;
  extensions: AccessMode;
};

const fileTools = ["read", "grep", "find", "ls", "edit", "write"] as const;
const accessModes: AccessMode[] = ["deny", "ask", "allow"];
const lines = (value: string) =>
  value
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
const copy = (config: Record<string, unknown>) => structuredClone(config);

function SelectField({
  id,
  label,
  value,
  options,
  onChange,
  hint,
}: {
  id: string;
  label: string;
  value: string;
  options: string[];
  onChange: (value: string) => void;
  hint?: string;
}) {
  return (
    <div className="form-field">
      <Label htmlFor={id}>{label}</Label>
      <select
        id={id}
        className="native-select"
        value={value}
        onChange={(event) => onChange(event.target.value)}
      >
        {options.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </select>
      {hint && <small>{hint}</small>}
    </div>
  );
}

export function ProfileManager({
  adapters,
  workspaces,
  onChanged,
  notify,
}: {
  adapters: AdapterInfo[];
  workspaces: Workspace[];
  onChanged: () => Promise<void>;
  notify: (message: string) => void;
}) {
  const [adapterId, setAdapterId] = useState(adapters[0]?.id || "");
  const adapter = adapters.find((item) => item.id === adapterId);
  const runtimeType = adapter?.runtime_type || "pi";
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [permissionProfiles, setPermissionProfiles] = useState<
    NativePermissionProfile[]
  >([]);
  const [contextWorkspaceId, setContextWorkspaceId] = useState<string | null>(
    null,
  );
  const [selectedId, setSelectedId] = useState("");
  const [mode, setMode] = useState<Mode>("choose");
  const [draftId, setDraftId] = useState("");
  const [draft, setDraft] = useState<Record<string, unknown>>({});
  const [protectedText, setProtectedText] = useState("");
  const [exceptionText, setExceptionText] = useState("");
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const eligibleWorkspaces = adapter?.node_id
    ? workspaces.filter(
        (workspace) =>
          Boolean(workspace.node_id) && workspace.node_id === adapter.node_id,
      )
    : [];
  const effectiveContextWorkspaceId =
    contextWorkspaceId === ""
      ? ""
      : eligibleWorkspaces.find(
          (workspace) => workspace.id === contextWorkspaceId,
        )?.id ||
        eligibleWorkspaces[0]?.id ||
        "";
  const selected = profiles.find((profile) => profile.id === selectedId);
  const selectedConfig = selected?.config;
  const available = Boolean(adapter);
  const assignedWorkspaces = workspaces.filter(
    (workspace) => workspace.routes?.[adapterId]?.profile?.id === selectedId,
  );
  const pi = draft as unknown as PiConfig;
  const codex = draft as unknown as CodexConfig;
  const claude = draft as unknown as ClaudeConfig;

  useEffect(() => {
    if (!available && adapters[0]) setAdapterId(adapters[0].id);
  }, [available, adapters]);

  const profilesUrl = useCallback(() => {
    if (adapterId && effectiveContextWorkspaceId) {
      const query = new URLSearchParams({
        workspace_id: effectiveContextWorkspaceId,
        fresh: "1",
      });
      return `/api/adapters/${adapterId}/profiles?${query.toString()}`;
    }
    return `/api/adapters/${adapterId}/profiles`;
  }, [adapterId, effectiveContextWorkspaceId]);

  useEffect(() => {
    let live = true;
    if (!available) return;
    setProfiles([]);
    setSelectedId("");
    setMode("choose");
    setError("");
    api<{
      profiles: Profile[];
      permissionProfiles?: NativePermissionProfile[];
    }>(profilesUrl())
      .then((data) => {
        if (!live) return;
        setProfiles(data.profiles || []);
        setPermissionProfiles(data.permissionProfiles || []);
        setSelectedId(data.profiles?.[0]?.id || "");
      })
      .catch((failure) => {
        if (live) setError((failure as Error).message);
      });
    return () => {
      live = false;
    };
  }, [adapterId, available, effectiveContextWorkspaceId, profilesUrl]);

  async function reload(selectId: string) {
    const data = await api<{
      profiles: Profile[];
      permissionProfiles?: NativePermissionProfile[];
    }>(profilesUrl());
    setProfiles(data.profiles || []);
    setPermissionProfiles(data.permissionProfiles || []);
    setSelectedId(selectId);
  }

  async function saveDefinition() {
    if (!/^[a-z][a-z0-9_-]{0,63}$/.test(draftId)) {
      setError(
        "Use a lowercase ID with letters, numbers, hyphens or underscores.",
      );
      return;
    }
    setBusy(true);
    setError("");
    try {
      const saved = await api<Profile>(
        `/api/adapters/${adapterId}/profiles`,
        "POST",
        {
          id: draftId,
          config: draft,
          expected_revision:
            mode === "edit"
              ? (selected?.definitionRevision ?? selected?.revision)
              : null,
        },
      );
      await reload(saved.id);
      await onChanged();
      setMode("choose");
      if (mode === "create") {
        notify(`Profile ${saved.id} created. Assign it from a workspace.`);
      } else {
        notify(
          `Profile ${saved.id} updated. Start a new conversation to use it.`,
        );
      }
    } catch (failure) {
      setError((failure as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!selected) return;
    setBusy(true);
    setError("");
    try {
      await api(`/api/adapters/${adapterId}/profiles/${selected.id}`, "DELETE");
      await reload(
        profiles.find((profile) => profile.id !== selected.id)?.id || "",
      );
      notify(`Profile ${selected.id} deleted.`);
    } catch (failure) {
      setError((failure as Error).message);
    } finally {
      setDeleteOpen(false);
      setBusy(false);
    }
  }

  function begin(mode: "create" | "edit") {
    if (!selected || !selectedConfig) return;
    setDraftId(mode === "edit" ? selected.id : "");
    setDraft(copy(selectedConfig));
    if (runtimeType === "pi") {
      const config = selectedConfig as unknown as PiConfig;
      setProtectedText((config.protected_patterns || []).join("\n"));
      setExceptionText((config.protected_template_exceptions || []).join("\n"));
    }
    setError("");
    setMode(mode);
  }

  function updatePi(changes: Partial<PiConfig>) {
    setDraft((current) => ({ ...current, ...changes }));
  }

  return (
    <>
      <div className="profiles-page">
        <div
          className="profiles-runtime-tabs"
          role="group"
          aria-label="Adapter"
        >
          {adapters.map((item) => (
            <button
              key={item.id}
              type="button"
              aria-pressed={adapterId === item.id}
              className={adapterId === item.id ? "current" : ""}
              onClick={() => setAdapterId(item.id)}
            >
              {item.name} · {runtimeName(item.runtime_type)}
            </button>
          ))}
        </div>
        {adapters.length === 0 ? (
          <p className="profiles-empty">
            Add an adapter to manage its security profiles.
          </p>
        ) : (
          <div className="profiles-layout">
            <aside
              className="profiles-list-panel"
              aria-label={`${adapter?.name || "Adapter"} profiles`}
            >
              <h2>Saved profiles</h2>
              <p>Choose one to inspect or use as a starting point.</p>
              <div className="profiles-list">
                {profiles.map((profile) => (
                  <button
                    key={profile.id}
                    type="button"
                    className={`profile-list-item ${selectedId === profile.id ? "current" : ""}`}
                    aria-current={
                      selectedId === profile.id ? "true" : undefined
                    }
                    onClick={() => {
                      setSelectedId(profile.id);
                      setMode("choose");
                      setError("");
                    }}
                  >
                    <strong>{profile.id}</strong>
                    <span>
                      {profile.mutable ? "Custom" : "Built in"}
                      {profile.available === false ? " · unavailable here" : ""}
                    </span>
                  </button>
                ))}
              </div>
            </aside>
            <section
              className="profiles-detail-panel"
              aria-label="Profile controls"
            >
              <div className="profiles-detail-head">
                <div>
                  <h2>
                    {mode === "create"
                      ? "Create profile"
                      : mode === "edit"
                        ? `Edit ${draftId}`
                        : selected?.id || "Select a profile"}
                  </h2>
                  <p>
                    {mode === "choose"
                      ? "Profiles are shared across workspaces. Assign one from a workspace."
                      : "Changes apply to new conversations. Existing conversations keep their profile revision."}
                  </p>
                </div>
              </div>
              {mode === "choose" ? (
                <div className="profile-form">
                  {selected && (
                    <div className="profile-summary">
                      <strong>
                        {selected.mutable
                          ? "Custom profile"
                          : "Built-in starting point"}
                      </strong>
                      <span>Revision {selected.revision.slice(0, 12)}</span>
                      <span>
                        {selectedConfig
                          ? runtimeType === "pi"
                            ? `Files outside workspace: ${(selectedConfig as unknown as PiConfig).external_access?.default_mode || "deny"}`
                            : runtimeType === "claude"
                              ? `Edits ${(selectedConfig as unknown as ClaudeConfig).edits || "deny"} · shell ${(selectedConfig as unknown as ClaudeConfig).shell || "deny"} · web ${(selectedConfig as unknown as ClaudeConfig).web || "deny"} · extensions ${(selectedConfig as unknown as ClaudeConfig).extensions || "deny"}`
                              : `Permission profile: ${(selectedConfig as unknown as CodexConfig).permissions || "unknown"}`
                          : "Controls unavailable from the installed adapter"}
                      </span>
                      <span>
                        Assigned to {assignedWorkspaces.length} workspace
                        {assignedWorkspaces.length === 1 ? "" : "s"}
                        {assignedWorkspaces.length > 0 &&
                          `: ${assignedWorkspaces.map((workspace) => workspace.name).join(", ")}`}
                      </span>
                    </div>
                  )}
                  {selected && !selectedConfig && (
                    <p className="profile-error" role="status">
                      Update the native {runtimeName(runtimeType)} adapter to
                      create or edit profiles. Existing profiles can still be
                      assigned.
                    </p>
                  )}
                  <div className="profile-actions">
                    <Button
                      variant="outline"
                      onClick={() => begin("create")}
                      disabled={!selectedConfig || busy}
                    >
                      Create from selected
                    </Button>
                    {selected?.mutable && (
                      <>
                        <Button
                          variant="outline"
                          onClick={() => begin("edit")}
                          disabled={!selectedConfig || busy}
                        >
                          Edit controls
                        </Button>
                        <Button
                          variant="outline"
                          onClick={() => setDeleteOpen(true)}
                          disabled={busy || assignedWorkspaces.length > 0}
                        >
                          Delete
                        </Button>
                      </>
                    )}
                  </div>
                  <small>
                    To delete an assigned profile, assign another profile in
                    every workspace first.
                  </small>
                </div>
              ) : (
                <div className="profile-form">
                  {mode === "create" && (
                    <div className="form-field">
                      <Label htmlFor="profile-id">Profile ID</Label>
                      <Input
                        id="profile-id"
                        value={draftId}
                        onChange={(event) => setDraftId(event.target.value)}
                        placeholder="reviewed-external-files"
                        maxLength={64}
                      />
                    </div>
                  )}
                  {runtimeType === "codex" && (
                    <div className="form-field">
                      <Label htmlFor="codex-discovery-workspace">
                        Native profile choices
                      </Label>
                      <select
                        id="codex-discovery-workspace"
                        className="native-select"
                        value={effectiveContextWorkspaceId}
                        onChange={(event) =>
                          setContextWorkspaceId(event.target.value)
                        }
                      >
                        <option value="">No workspace context</option>
                        {eligibleWorkspaces.map((workspace) => (
                          <option key={workspace.id} value={workspace.id}>
                            {workspace.name}
                          </option>
                        ))}
                      </select>
                      <small>
                        Native profile choices are discovered for this exact
                        adapter and workspace.
                      </small>
                    </div>
                  )}
                  {runtimeType === "pi" ? (
                    <>
                      <section className="profile-section">
                        <h3>File tools</h3>
                        <div className="profile-toggle">
                          <div>
                            <strong>Enable edit and write</strong>
                            <small>
                              Tool modes below decide whether each action asks.
                            </small>
                          </div>
                          <Switch
                            aria-label="Enable edit and write"
                            checked={pi.write_tools_enabled}
                            onCheckedChange={(value) =>
                              updatePi({ write_tools_enabled: value })
                            }
                          />
                        </div>
                        <div className="profile-grid">
                          {fileTools.map((tool) => (
                            <SelectField
                              key={tool}
                              id={`profile-tool-${tool}`}
                              label={tool}
                              value={pi.tools?.[tool] || "deny"}
                              options={accessModes}
                              onChange={(value) =>
                                updatePi({
                                  tools: {
                                    ...pi.tools,
                                    [tool]: value as AccessMode,
                                  },
                                })
                              }
                            />
                          ))}
                        </div>
                      </section>
                      <section className="profile-section">
                        <h3>Outside the workspace</h3>
                        <SelectField
                          id="profile-external-default"
                          label="Default file access"
                          value={pi.external_access?.default_mode || "deny"}
                          options={accessModes}
                          onChange={(value) =>
                            updatePi({
                              external_access: {
                                ...pi.external_access,
                                default_mode: value as AccessMode,
                              },
                            })
                          }
                          hint="Applies to Pi file tools; shell and extensions have separate authority."
                        />
                        {(pi.external_access?.roots || []).map(
                          (root, index) => (
                            <div className="profile-root-row" key={index}>
                              <Input
                                aria-label={`External root ${index + 1}`}
                                value={root.path}
                                placeholder="/absolute/host/path"
                                onChange={(event) => {
                                  const roots = [...pi.external_access.roots];
                                  roots[index] = {
                                    ...root,
                                    path: event.target.value,
                                  };
                                  updatePi({
                                    external_access: {
                                      ...pi.external_access,
                                      roots,
                                    },
                                  });
                                }}
                              />
                              <select
                                className="native-select"
                                aria-label={`Access for external root ${index + 1}`}
                                value={root.mode}
                                onChange={(event) => {
                                  const roots = [...pi.external_access.roots];
                                  roots[index] = {
                                    ...root,
                                    mode: event.target.value as AccessMode,
                                  };
                                  updatePi({
                                    external_access: {
                                      ...pi.external_access,
                                      roots,
                                    },
                                  });
                                }}
                              >
                                {accessModes.map((value) => (
                                  <option key={value}>{value}</option>
                                ))}
                              </select>
                              <Button
                                variant="outline"
                                onClick={() =>
                                  updatePi({
                                    external_access: {
                                      ...pi.external_access,
                                      roots: pi.external_access.roots.filter(
                                        (_, i) => i !== index,
                                      ),
                                    },
                                  })
                                }
                              >
                                Remove
                              </Button>
                            </div>
                          ),
                        )}
                        <Button
                          variant="outline"
                          onClick={() =>
                            updatePi({
                              external_access: {
                                ...pi.external_access,
                                roots: [
                                  ...pi.external_access.roots,
                                  { path: "", mode: "ask" },
                                ],
                              },
                            })
                          }
                          disabled={
                            (pi.external_access?.roots.length || 0) >= 32
                          }
                        >
                          Add external root
                        </Button>
                      </section>
                      <section className="profile-section">
                        <h3>Shell and protected files</h3>
                        <SelectField
                          id="profile-shell"
                          label="Shell commands"
                          value={pi.shell_mode || "deny"}
                          options={accessModes}
                          onChange={(value) =>
                            updatePi({ shell_mode: value as AccessMode })
                          }
                          hint="An approved shell command runs with the macOS user's file and network access."
                        />
                        <div className="form-field">
                          <Label htmlFor="profile-protected">
                            Protected paths
                          </Label>
                          <Textarea
                            id="profile-protected"
                            rows={3}
                            value={protectedText}
                            onChange={(event) => {
                              setProtectedText(event.target.value);
                              updatePi({
                                protected_patterns: lines(event.target.value),
                              });
                            }}
                          />
                          <small>
                            One workspace relative pattern per line.
                          </small>
                        </div>
                        <div className="form-field">
                          <Label htmlFor="profile-exceptions">
                            Template exceptions
                          </Label>
                          <Textarea
                            id="profile-exceptions"
                            rows={2}
                            value={exceptionText}
                            onChange={(event) => {
                              setExceptionText(event.target.value);
                              updatePi({
                                protected_template_exceptions: lines(
                                  event.target.value,
                                ),
                              });
                            }}
                          />
                        </div>
                        <div className="profile-toggle">
                          <div>
                            <strong>Session grants</strong>
                            <small>
                              Allow an approved exact target for the rest of
                              this session.
                            </small>
                          </div>
                          <Switch
                            aria-label="Session grants"
                            checked={pi.allow_session_always}
                            onCheckedChange={(value) =>
                              updatePi({ allow_session_always: value })
                            }
                          />
                        </div>
                      </section>
                    </>
                  ) : runtimeType === "claude" ? (
                    <section className="profile-section">
                      <h3>Claude Code security profile</h3>
                      <small>
                        Reading inside the workspace is always allowed. Git
                        internals, <code>.env</code> files, and paths outside
                        the workspace are always denied, and edits never reach{" "}
                        <code>.claude</code> or <code>.mcp.json</code>.
                      </small>
                      <SelectField
                        id="profile-claude-edits"
                        label="File edits"
                        value={claude.edits || "deny"}
                        options={accessModes}
                        onChange={(value) =>
                          setDraft((current) => ({ ...current, edits: value }))
                        }
                        hint="Edit, Write, and notebook edits inside the workspace."
                      />
                      <SelectField
                        id="profile-claude-shell"
                        label="Shell commands"
                        value={claude.shell || "deny"}
                        options={accessModes}
                        onChange={(value) =>
                          setDraft((current) => ({ ...current, shell: value }))
                        }
                        hint="Allowed commands run with this host user's authority and are not sandboxed. Prefer ask."
                      />
                      <SelectField
                        id="profile-claude-web"
                        label="Web access"
                        value={claude.web || "deny"}
                        options={accessModes}
                        onChange={(value) =>
                          setDraft((current) => ({ ...current, web: value }))
                        }
                        hint="Web fetch and web search."
                      />
                      <SelectField
                        id="profile-claude-extensions"
                        label="MCP extensions"
                        value={claude.extensions || "deny"}
                        options={accessModes}
                        onChange={(value) =>
                          setDraft((current) => ({
                            ...current,
                            extensions: value,
                          }))
                        }
                        hint="Tools from MCP servers and plugins. Deny also stops those servers from starting. Sub-agents and skills are available, and every tool they call follows this profile."
                      />
                    </section>
                  ) : (
                    <>
                      <section className="profile-section">
                        <h3>Codex security profile</h3>
                        {permissionProfiles.length > 0 && (
                          <div className="form-field">
                            <Label htmlFor="profile-native-permission">
                              Permission profile
                            </Label>
                            <select
                              id="profile-native-permission"
                              className="native-select"
                              value={
                                permissionProfiles.some(
                                  (profile) => profile.id === codex.permissions,
                                )
                                  ? codex.permissions
                                  : "__custom__"
                              }
                              onChange={(event) =>
                                setDraft((current) => ({
                                  ...current,
                                  permissions:
                                    event.target.value === "__custom__"
                                      ? ""
                                      : event.target.value,
                                }))
                              }
                            >
                              {permissionProfiles.map((profile) => (
                                <option key={profile.id} value={profile.id}>
                                  {profile.id}
                                  {profile.description
                                    ? " — " + profile.description
                                    : ""}
                                </option>
                              ))}
                              <option value="__custom__">
                                Enter a named profile ID
                              </option>
                            </select>
                            <small>
                              Choices come from Codex for{" "}
                              {workspaces.find(
                                (workspace) =>
                                  workspace.id === effectiveContextWorkspaceId,
                              )?.name || "the selected workspace"}
                              .
                            </small>
                          </div>
                        )}
                        {!permissionProfiles.some(
                          (profile) => profile.id === codex.permissions,
                        ) && (
                          <div className="form-field">
                            <Label htmlFor="profile-permissions">
                              Permission profile ID
                            </Label>
                            <Input
                              id="profile-permissions"
                              value={codex.permissions || ""}
                              onChange={(event) =>
                                setDraft((current) => ({
                                  ...current,
                                  permissions: event.target.value,
                                }))
                              }
                              placeholder=":workspace or workspace-net"
                              maxLength={128}
                              autoComplete="off"
                            />
                            <small>
                              Codex defines filesystem, network, domain, and
                              socket rules in its permission profiles and
                              config.toml. Bridge selects this ID and verifies
                              it against the target workspace when assigned.
                            </small>
                          </div>
                        )}
                        <SelectField
                          id="profile-approval"
                          label="Approvals"
                          value={codex.approvalPolicy || "on-request"}
                          options={["on-request", "never"]}
                          onChange={(value) =>
                            setDraft((current) => ({
                              ...current,
                              approvalPolicy: value,
                              approvalsReviewer:
                                value === "never"
                                  ? "user"
                                  : current.approvalsReviewer,
                            }))
                          }
                        />
                        <SelectField
                          id="profile-reviewer"
                          label="Reviewer"
                          value={codex.approvalsReviewer || "user"}
                          options={
                            codex.approvalPolicy === "never"
                              ? ["user"]
                              : ["user", "auto_review"]
                          }
                          onChange={(value) =>
                            setDraft((current) => ({
                              ...current,
                              approvalsReviewer: value,
                            }))
                          }
                          hint="User review sends approval requests to the Bridge interaction flow."
                        />
                      </section>
                    </>
                  )}
                </div>
              )}

              {error && (
                <p className="profile-error" role="alert">
                  {error}
                </p>
              )}
              {mode !== "choose" && (
                <div className="profile-editor-actions">
                  <Button
                    variant="outline"
                    onClick={() => {
                      setMode("choose");
                      setError("");
                    }}
                    disabled={busy}
                  >
                    Back
                  </Button>
                  <Button onClick={() => void saveDefinition()} disabled={busy}>
                    {mode === "create" ? "Create profile" : "Save controls"}
                  </Button>
                </div>
              )}
            </section>
          </div>
        )}
      </div>
      <AlertDialog open={deleteOpen} onOpenChange={setDeleteOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete {selected?.id}?</AlertDialogTitle>
            <AlertDialogDescription>
              Existing conversations using this profile cannot continue after
              deletion. Assign another profile in every workspace first.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={() => void remove()}>
              Delete profile
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
