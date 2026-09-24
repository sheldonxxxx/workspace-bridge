import { useEffect, useState } from "react";
import { api, runtimeName, type Workspace } from "@/lib/api";
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
  mutable: boolean;
  config: Record<string, unknown> | null;
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
  sandbox: "read-only" | "workspace-write" | "danger-full-access";
  approvalPolicy: "on-request" | "never";
  approvalsReviewer: "user" | "auto_review";
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
  runtimes,
  workspaces,
  onChanged,
  notify,
}: {
  runtimes: string[];
  workspaces: Workspace[];
  onChanged: () => Promise<void>;
  notify: (message: string) => void;
}) {
  const [runtime, setRuntime] = useState(runtimes[0] || "pi");
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [mode, setMode] = useState<Mode>("choose");
  const [draftId, setDraftId] = useState("");
  const [draft, setDraft] = useState<Record<string, unknown>>({});
  const [protectedText, setProtectedText] = useState("");
  const [exceptionText, setExceptionText] = useState("");
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const selected = profiles.find((profile) => profile.id === selectedId);
  const selectedConfig = selected?.config;
  const available = runtimes.includes(runtime);
  const firstRuntime = runtimes[0];
  const assignedWorkspaces = workspaces.filter(
    (workspace) =>
      workspace.runtime_grants?.[runtime]?.profile?.id === selectedId,
  );
  const pi = draft as unknown as PiConfig;
  const codex = draft as unknown as CodexConfig;

  useEffect(() => {
    if (!available && firstRuntime) setRuntime(firstRuntime);
  }, [available, firstRuntime]);

  useEffect(() => {
    let live = true;
    if (!available) return;
    setProfiles([]);
    setSelectedId("");
    setMode("choose");
    setError("");
    api<{ profiles: Profile[] }>(`/api/runtimes/${runtime}/profiles`)
      .then((data) => {
        if (!live) return;
        setProfiles(data.profiles || []);
        setSelectedId(data.profiles?.[0]?.id || "");
      })
      .catch((failure) => {
        if (live) setError((failure as Error).message);
      });
    return () => {
      live = false;
    };
  }, [runtime, available]);

  async function reload(selectId: string) {
    const data = await api<{ profiles: Profile[] }>(
      `/api/runtimes/${runtime}/profiles`,
    );
    setProfiles(data.profiles || []);
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
        `/api/runtimes/${runtime}/profiles`,
        "POST",
        {
          id: draftId,
          config: draft,
          expected_revision: mode === "edit" ? selected?.revision : null,
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
      await api(`/api/runtimes/${runtime}/profiles/${selected.id}`, "DELETE");
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
    if (runtime === "pi") {
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
        <div className="profiles-runtime-tabs" role="group" aria-label="Runtime">
          {runtimes.map((id) => (
            <button
              key={id}
              type="button"
              aria-pressed={runtime === id}
              className={runtime === id ? "current" : ""}
              onClick={() => setRuntime(id)}
            >
              {runtimeName(id)}
            </button>
          ))}
        </div>
        {runtimes.length === 0 ? (
          <p className="profiles-empty">
            Connect a runtime to manage its security profiles.
          </p>
        ) : (
          <div className="profiles-layout">
            <aside
              className="profiles-list-panel"
              aria-label={`${runtimeName(runtime)} profiles`}
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
                    <span>{profile.mutable ? "Custom" : "Built in"}</span>
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
                          ? runtime === "pi"
                            ? `Files outside workspace: ${(selectedConfig as unknown as PiConfig).external_access?.default_mode || "deny"}`
                            : `Sandbox: ${(selectedConfig as unknown as CodexConfig).sandbox || "unknown"}`
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
                      Update the native {runtimeName(runtime)} adapter to create
                      or edit profiles. Existing profiles can still be assigned.
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
                  {runtime === "pi" ? (
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
                  ) : (
                    <>
                      <section className="profile-section">
                        <h3>Codex controls</h3>
                        <SelectField
                          id="profile-sandbox"
                          label="Sandbox"
                          value={codex.sandbox || "read-only"}
                          options={[
                            "read-only",
                            "workspace-write",
                            "danger-full-access",
                          ]}
                          onChange={(value) =>
                            setDraft((current) => ({
                              ...current,
                              sandbox: value,
                            }))
                          }
                          hint="Workspace write confines routine edits to the workspace. Full access removes that boundary."
                        />
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
