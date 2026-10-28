import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
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

type AccessMode = "deny" | "ask" | "allow";
type RootRule = { path: string; read?: AccessMode; write?: AccessMode };
type CommandRule = { prefix: string; mode: AccessMode };
type Rules = Record<(typeof categories)[number]["key"], AccessMode> & {
  protected: string[];
  external_roots: RootRule[];
  commands: CommandRule[];
};
type RouteUse = {
  workspace_id: string;
  workspace_name: string;
  adapter_id: string;
  adapter_name: string;
  enabled: boolean;
};
type Profile = {
  id: string;
  revision: string;
  builtin: boolean;
  description?: string;
  rules: Rules;
  routes: RouteUse[];
};
type Confirmation = {
  loosened: string[];
  routes: RouteUse[];
  unenforceable_routes: Array<RouteUse & { unenforced: string[] }>;
};
type Draft = {
  id: string;
  categories: Record<string, AccessMode>;
  protectedText: string;
  roots: Array<{ path: string; read: string; write: string }>;
  commands: CommandRule[];
};
type Mode = "choose" | "create" | "edit";

const modes: AccessMode[] = ["deny", "ask", "allow"];
const categories = [
  { key: "read_inside", label: "Read inside the workspace" },
  { key: "read_outside", label: "Read outside the workspace" },
  { key: "write_inside", label: "Edit inside the workspace" },
  { key: "write_outside", label: "Write outside the workspace" },
  { key: "shell", label: "Run shell commands" },
  { key: "web", label: "Web fetch and search" },
  { key: "extensions", label: "Extensions and extra tools" },
] as const;
const keyLabels: Record<string, string> = {
  ...Object.fromEntries(categories.map((item) => [item.key, item.label])),
  protected: "Protected paths",
  external_roots: "Outside folders",
  commands: "Command rules",
};
const labelKeys = (keys: string[]) =>
  keys.map((key) => keyLabels[key] || key).join(", ");

function toDraft(id: string, rules: Rules): Draft {
  return {
    id,
    categories: Object.fromEntries(
      categories.map((item) => [item.key, rules[item.key]]),
    ),
    protectedText: rules.protected.join("\n"),
    roots: rules.external_roots.map((root) => ({
      path: root.path,
      read: root.read || "",
      write: root.write || "",
    })),
    commands: rules.commands.map((rule) => ({ ...rule })),
  };
}

function toRules(draft: Draft): Record<string, unknown> {
  return {
    ...draft.categories,
    protected: draft.protectedText
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean),
    external_roots: draft.roots
      .filter((root) => root.path.trim())
      .map((root) => ({
        path: root.path.trim(),
        ...(root.read ? { read: root.read } : {}),
        ...(root.write ? { write: root.write } : {}),
      })),
    commands: draft.commands
      .filter((rule) => rule.prefix.trim())
      .map((rule) => ({ prefix: rule.prefix.trim(), mode: rule.mode })),
  };
}

function ModeSelect({
  id,
  value,
  onChange,
  inherit,
}: {
  id: string;
  value: string;
  onChange: (value: string) => void;
  inherit?: boolean;
}) {
  return (
    <select
      id={id}
      className="native-select"
      value={value}
      onChange={(event) => onChange(event.target.value)}
    >
      {inherit && <option value="">Same as outside</option>}
      {modes.map((mode) => (
        <option key={mode} value={mode}>
          {mode}
        </option>
      ))}
    </select>
  );
}

export function ProfileManager({
  onChanged,
  notify,
}: {
  onChanged: () => Promise<void>;
  notify: (message: string) => void;
}) {
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [mode, setMode] = useState<Mode>("choose");
  const [draft, setDraft] = useState<Draft | null>(null);
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const selected = profiles.find((profile) => profile.id === selectedId);

  const load = useCallback(async (selectId?: string) => {
    const data = await api<{ profiles: Profile[] }>("/api/profiles");
    const rows = data.profiles || [];
    setProfiles(rows);
    setSelectedId((current) => {
      const wanted = selectId ?? current;
      return rows.some((row) => row.id === wanted) ? wanted : rows[0]?.id || "";
    });
  }, []);

  useEffect(() => {
    let live = true;
    load()
      .catch((failure) => {
        if (live) setError((failure as Error).message);
      })
      .finally(() => {
        if (live) setLoading(false);
      });
    return () => {
      live = false;
    };
  }, [load]);

  function begin(next: "create" | "edit") {
    if (!selected) return;
    setDraft(toDraft(next === "edit" ? selected.id : "", selected.rules));
    setError("");
    setMode(next);
  }

  function update(changes: Partial<Draft>) {
    setDraft((current) => (current ? { ...current, ...changes } : current));
  }

  async function save(confirm: boolean) {
    if (!draft) return;
    if (!/^[a-z][a-z0-9_-]{0,63}$/.test(draft.id)) {
      setError(
        "Use a lowercase ID with letters, numbers, hyphens or underscores.",
      );
      return;
    }
    setBusy(true);
    setError("");
    try {
      const result = await api<
        Partial<Confirmation> & {
          saved?: boolean;
          needs_confirmation?: boolean;
          id: string;
        }
      >(`/api/profiles/${draft.id}`, "PUT", {
        rules: toRules(draft),
        expected_revision: mode === "edit" ? selected?.revision : null,
        confirm,
      });
      if (result.needs_confirmation) {
        setConfirmation({
          loosened: result.loosened || [],
          routes: result.routes || [],
          unenforceable_routes: result.unenforceable_routes || [],
        });
        return;
      }
      setConfirmation(null);
      await load(draft.id);
      await onChanged();
      notify(
        mode === "create"
          ? `Profile ${draft.id} created. Assign it from a workspace.`
          : `Profile ${draft.id} updated. New runs use it; a run in progress keeps its rules.`,
      );
      setMode("choose");
    } catch (failure) {
      setConfirmation(null);
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
      await api(`/api/profiles/${selected.id}`, "DELETE");
      await load(profiles.find((row) => row.id !== selected.id)?.id || "");
      await onChanged();
      notify(`Profile ${selected.id} deleted.`);
    } catch (failure) {
      setError((failure as Error).message);
    } finally {
      setDeleteOpen(false);
      setBusy(false);
    }
  }

  return (
    <>
      <div className="profiles-page">
        <div className="profiles-layout">
          <aside className="profiles-list-panel" aria-label="Profiles">
            <h2>Saved profiles</h2>
            <p>Choose one to inspect or use as a starting point.</p>
            <div className="profiles-list">
              {profiles.map((profile) => (
                <button
                  key={profile.id}
                  type="button"
                  className={`profile-list-item ${selectedId === profile.id ? "current" : ""}`}
                  aria-current={selectedId === profile.id ? "true" : undefined}
                  onClick={() => {
                    setSelectedId(profile.id);
                    setMode("choose");
                    setError("");
                  }}
                >
                  <strong>{profile.id}</strong>
                  <span>{profile.builtin ? "Built in" : "Custom"}</span>
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
                      ? `Edit ${draft?.id}`
                      : selected?.id || (loading ? "Loading" : "No profile")}
                </h2>
                <p>
                  {mode === "choose"
                    ? "One format for every agent. Assign a profile to a workspace's route; each agent enforces what it can."
                    : "Edits apply to new runs, and to continuations at the next idle point. A run in progress keeps its rules."}
                </p>
              </div>
            </div>
            {mode === "choose" ? (
              <div className="profile-form">
                {selected && (
                  <>
                    <div className="profile-summary">
                      <strong>
                        {selected.builtin
                          ? "Built-in starting point"
                          : "Custom profile"}
                      </strong>
                      <span>Revision {selected.revision.slice(0, 17)}</span>
                      {selected.description && (
                        <span>{selected.description}</span>
                      )}
                      <span>
                        Used by {selected.routes.length} route
                        {selected.routes.length === 1 ? "" : "s"}
                        {selected.routes.length > 0 &&
                          `: ${selected.routes
                            .map(
                              (route) =>
                                `${route.workspace_name} · ${route.adapter_name}`,
                            )
                            .join(", ")}`}
                      </span>
                    </div>
                    <table className="profile-rules" aria-label="Rules">
                      <tbody>
                        {categories.map((item) => (
                          <tr key={item.key}>
                            <th scope="row">{item.label}</th>
                            <td>{selected.rules[item.key]}</td>
                          </tr>
                        ))}
                        <tr>
                          <th scope="row">Protected paths</th>
                          <td>
                            {selected.rules.protected.length
                              ? selected.rules.protected.join(", ")
                              : "Only the fixed protected paths"}
                          </td>
                        </tr>
                        <tr>
                          <th scope="row">Outside folders</th>
                          <td>
                            {selected.rules.external_roots.length
                              ? selected.rules.external_roots
                                  .map(
                                    (root) =>
                                      `${root.path} (read ${root.read || "default"}, write ${root.write || "default"})`,
                                  )
                                  .join("; ")
                              : "None"}
                          </td>
                        </tr>
                        <tr>
                          <th scope="row">Command rules</th>
                          <td>
                            {selected.rules.commands.length
                              ? selected.rules.commands
                                  .map((rule) => `${rule.prefix}: ${rule.mode}`)
                                  .join("; ")
                              : "None"}
                          </td>
                        </tr>
                      </tbody>
                    </table>
                  </>
                )}
                <div className="profile-actions">
                  <Button
                    variant="outline"
                    onClick={() => begin("create")}
                    disabled={!selected || busy}
                  >
                    Create from selected
                  </Button>
                  {selected && !selected.builtin && (
                    <>
                      <Button
                        variant="outline"
                        onClick={() => begin("edit")}
                        disabled={busy}
                      >
                        Edit rules
                      </Button>
                      <Button
                        variant="outline"
                        onClick={() => setDeleteOpen(true)}
                        disabled={busy || selected.routes.length > 0}
                      >
                        Delete
                      </Button>
                    </>
                  )}
                </div>
                <small>
                  To delete an assigned profile, assign another profile on every
                  route that uses it first.
                </small>
                {error && (
                  <p className="profile-error" role="alert">
                    {error}
                  </p>
                )}
              </div>
            ) : (
              draft && (
                <div className="profile-form">
                  {mode === "create" && (
                    <div className="form-field">
                      <Label htmlFor="profile-id">Profile ID</Label>
                      <Input
                        id="profile-id"
                        value={draft.id}
                        onChange={(event) => update({ id: event.target.value })}
                        placeholder="reviewed-external-files"
                        maxLength={64}
                      />
                    </div>
                  )}
                  <section className="profile-section">
                    <h3>What the agent may do</h3>
                    <div className="profile-grid">
                      {categories.map((item) => (
                        <div className="form-field" key={item.key}>
                          <Label htmlFor={`rule-${item.key}`}>
                            {item.label}
                          </Label>
                          <ModeSelect
                            id={`rule-${item.key}`}
                            value={draft.categories[item.key]}
                            onChange={(value) =>
                              update({
                                categories: {
                                  ...draft.categories,
                                  [item.key]: value as AccessMode,
                                },
                              })
                            }
                          />
                        </div>
                      ))}
                    </div>
                    <small>
                      deny blocks the action, ask sends an approval request to
                      ChatGPT, allow lets it through.
                    </small>
                  </section>
                  <section className="profile-section">
                    <h3>Protected paths</h3>
                    <Label htmlFor="rule-protected">
                      Workspace-relative globs, one per line
                    </Label>
                    <Textarea
                      id="rule-protected"
                      rows={4}
                      value={draft.protectedText}
                      onChange={(event) =>
                        update({ protectedText: event.target.value })
                      }
                      placeholder={"secrets/**\n*.pem"}
                    />
                    <small>
                      Git internals, environment files and checks.toml are
                      always protected.
                    </small>
                  </section>
                  <section className="profile-section">
                    <h3>Outside folders</h3>
                    {draft.roots.map((root, index) => (
                      <div className="profile-root-row" key={index}>
                        <Input
                          aria-label={`Folder ${index + 1} path`}
                          value={root.path}
                          placeholder="/path/or/~/folder"
                          onChange={(event) =>
                            update({
                              roots: draft.roots.map((item, at) =>
                                at === index
                                  ? { ...item, path: event.target.value }
                                  : item,
                              ),
                            })
                          }
                        />
                        <ModeSelect
                          id={`root-read-${index}`}
                          inherit
                          value={root.read}
                          onChange={(value) =>
                            update({
                              roots: draft.roots.map((item, at) =>
                                at === index ? { ...item, read: value } : item,
                              ),
                            })
                          }
                        />
                        <ModeSelect
                          id={`root-write-${index}`}
                          inherit
                          value={root.write}
                          onChange={(value) =>
                            update({
                              roots: draft.roots.map((item, at) =>
                                at === index ? { ...item, write: value } : item,
                              ),
                            })
                          }
                        />
                        <Button
                          variant="outline"
                          type="button"
                          onClick={() =>
                            update({
                              roots: draft.roots.filter(
                                (_, at) => at !== index,
                              ),
                            })
                          }
                        >
                          Remove
                        </Button>
                      </div>
                    ))}
                    <Button
                      variant="outline"
                      type="button"
                      onClick={() =>
                        update({
                          roots: [
                            ...draft.roots,
                            { path: "", read: "", write: "" },
                          ],
                        })
                      }
                    >
                      Add folder
                    </Button>
                    <small>
                      Each folder sets read then write access, overriding the
                      outside-the-workspace rules above.
                    </small>
                  </section>
                  <section className="profile-section">
                    <h3>Command rules</h3>
                    {draft.commands.map((rule, index) => (
                      <div className="profile-root-row" key={index}>
                        <Input
                          aria-label={`Command ${index + 1} prefix`}
                          value={rule.prefix}
                          placeholder="git push"
                          onChange={(event) =>
                            update({
                              commands: draft.commands.map((item, at) =>
                                at === index
                                  ? { ...item, prefix: event.target.value }
                                  : item,
                              ),
                            })
                          }
                        />
                        <ModeSelect
                          id={`command-mode-${index}`}
                          value={rule.mode}
                          onChange={(value) =>
                            update({
                              commands: draft.commands.map((item, at) =>
                                at === index
                                  ? { ...item, mode: value as AccessMode }
                                  : item,
                              ),
                            })
                          }
                        />
                        <Button
                          variant="outline"
                          type="button"
                          onClick={() =>
                            update({
                              commands: draft.commands.filter(
                                (_, at) => at !== index,
                              ),
                            })
                          }
                        >
                          Remove
                        </Button>
                      </div>
                    ))}
                    <Button
                      variant="outline"
                      type="button"
                      onClick={() =>
                        update({
                          commands: [
                            ...draft.commands,
                            { prefix: "", mode: "ask" },
                          ],
                        })
                      }
                    >
                      Add command rule
                    </Button>
                    <small>
                      A rule applies to any command that starts with its prefix.
                      Longer prefixes win; the shell rule covers the rest.
                    </small>
                  </section>
                  {error && (
                    <p className="profile-error" role="alert">
                      {error}
                    </p>
                  )}
                  <div className="profile-actions">
                    <Button onClick={() => save(false)} disabled={busy}>
                      {mode === "create" ? "Create profile" : "Save profile"}
                    </Button>
                    <Button
                      variant="outline"
                      onClick={() => {
                        setMode("choose");
                        setError("");
                      }}
                      disabled={busy}
                    >
                      Cancel
                    </Button>
                  </div>
                </div>
              )
            )}
          </section>
        </div>
      </div>
      <AlertDialog
        open={deleteOpen}
        onOpenChange={(open) => !busy && setDeleteOpen(open)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete {selected?.id}?</AlertDialogTitle>
            <AlertDialogDescription>
              No route uses this profile. Deleting it cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busy}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={(event) => {
                event.preventDefault();
                void remove();
              }}
              disabled={busy}
            >
              Delete profile
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
      <AlertDialog
        open={Boolean(confirmation)}
        onOpenChange={(open) => !open && !busy && setConfirmation(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Confirm this change</AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div>
                {confirmation && confirmation.loosened.length > 0 && (
                  <p>
                    This loosens {labelKeys(confirmation.loosened)}. It reaches
                    new runs on{" "}
                    {confirmation.routes.length
                      ? confirmation.routes
                          .map(
                            (route) =>
                              `${route.workspace_name} · ${route.adapter_name}`,
                          )
                          .join(", ")
                      : "no route yet"}
                    .
                  </p>
                )}
                {confirmation &&
                  confirmation.unenforceable_routes.length > 0 && (
                    <p>
                      Runs on these routes will be refused because the agent
                      cannot enforce the rules:{" "}
                      {confirmation.unenforceable_routes
                        .map(
                          (route) =>
                            `${route.workspace_name} · ${route.adapter_name} (${labelKeys(route.unenforced)})`,
                        )
                        .join(", ")}
                      .
                    </p>
                  )}
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busy}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={(event) => {
                event.preventDefault();
                void save(true);
              }}
              disabled={busy}
            >
              Save anyway
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
