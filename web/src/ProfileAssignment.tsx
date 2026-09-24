import { useEffect, useState } from "react";
import { api, runtimeName, type Workspace } from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

type Profile = {
  id: string;
  revision: string;
  mutable: boolean;
  available?: boolean;
};

type RuntimeConfigSecurity = {
  supported: boolean;
  available: boolean;
  status: string;
  revision: string;
  resolvedSummary?: {
    activePermissionProfile?: string | null;
    approvalPolicy?: string;
    approvalsReviewer?: string;
    provenance?: string;
  };
};

export function ProfileAssignment({
  workspace,
  runtime,
  onClose,
  onManage,
  onChanged,
  notify,
}: {
  workspace: Workspace;
  runtime: string;
  onClose: () => void;
  onManage: () => void;
  onChanged: () => Promise<void>;
  notify: (message: string) => void;
}) {
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [runtimeConfig, setRuntimeConfig] =
    useState<RuntimeConfigSecurity | null>(null);
  const [securitySource, setSecuritySource] = useState<
    "profile" | "runtime-config"
  >(workspace.runtime_grants?.[runtime]?.security_binding?.source || "profile");
  const [selectedId, setSelectedId] = useState(
    workspace.runtime_grants?.[runtime]?.profile?.id || "",
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let live = true;
    const query = new URLSearchParams({
      workspace_id: workspace.id,
      fresh: "1",
    });
    api<{ profiles: Profile[]; runtimeConfig?: RuntimeConfigSecurity }>(
      `/api/runtimes/${runtime}/profiles?${query.toString()}`,
    )
      .then((data) => {
        if (!live) return;
        const available = (data.profiles || []).filter(
          (profile) => profile.available !== false,
        );
        setProfiles(available);
        setSelectedId((current) =>
          available.some((profile) => profile.id === current)
            ? current
            : available[0]?.id || "",
        );
        setRuntimeConfig(data.runtimeConfig || null);
      })
      .catch((failure) => {
        if (live) setError((failure as Error).message);
      });
    return () => {
      live = false;
    };
  }, [runtime, workspace.id]);

  async function assign() {
    if (!selectedId) return;
    setBusy(true);
    setError("");
    try {
      await api(`/api/workspaces/${workspace.id}/runtimes/${runtime}`, "POST", {
        enabled: workspace.runtime_grants?.[runtime]?.enabled || false,
        security_source: securitySource,
        profile_id: securitySource === "profile" ? selectedId : null,
      });
      await onChanged();
      notify(
        securitySource === "profile"
          ? `Profile ${selectedId} assigned to ${workspace.name}.`
          : `Workspace ${workspace.name} will follow current Codex config.`,
      );
      onClose();
    } catch (failure) {
      setError((failure as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="profile-assignment-dialog">
        <DialogHeader>
          <DialogTitle>Change profile</DialogTitle>
          <DialogDescription>
            Choose a {runtimeName(runtime)} profile for {workspace.name}. New
            conversations use the selected profile. Codex choices are checked
            against this workspace's native permission-profile catalog.
          </DialogDescription>
        </DialogHeader>
        {runtime === "codex" && (
          <fieldset className="profile-assignment-modes">
            <legend>Security source</legend>
            <label className="profile-assignment-mode">
              <input
                type="radio"
                name="security-source"
                value="runtime-config"
                checked={securitySource === "runtime-config"}
                disabled={!runtimeConfig?.supported || !runtimeConfig.available}
                onChange={() => setSecuritySource("runtime-config")}
              />
              <span>
                <strong>Use Codex config (config.toml)</strong>
                <small>
                  Follow the current effective Codex security settings for this
                  workspace.
                </small>
              </span>
            </label>
            <label className="profile-assignment-mode">
              <input
                type="radio"
                name="security-source"
                value="profile"
                checked={securitySource === "profile"}
                onChange={() => setSecuritySource("profile")}
              />
              <span>
                <strong>Use Workspace Bridge profile</strong>
                <small>
                  Use an explicit Bridge profile revision for new conversations.
                </small>
              </span>
            </label>
            {securitySource === "runtime-config" && (
              <div className="runtime-config-summary" aria-live="polite">
                <strong>
                  {runtimeConfig?.available
                    ? "Codex config is available"
                    : "Codex config is unavailable"}
                </strong>
                {runtimeConfig?.resolvedSummary && (
                  <dl>
                    <div>
                      <dt>Permission profile</dt>
                      <dd>
                        {runtimeConfig.resolvedSummary
                          .activePermissionProfile || "Codex default"}
                      </dd>
                    </div>
                    <div>
                      <dt>Approval</dt>
                      <dd>{runtimeConfig.resolvedSummary.approvalPolicy}</dd>
                    </div>
                    <div>
                      <dt>Reviewer</dt>
                      <dd>{runtimeConfig.resolvedSummary.approvalsReviewer}</dd>
                    </div>
                    <div>
                      <dt>Provenance</dt>
                      <dd>{runtimeConfig.resolvedSummary.provenance}</dd>
                    </div>
                  </dl>
                )}
              </div>
            )}
          </fieldset>
        )}
        {securitySource === "profile" && (
          <div
            className="profile-assignment-list"
            role="group"
            aria-label="Available Workspace Bridge profiles"
          >
            {profiles.map((profile) => (
              <button
                key={profile.id}
                type="button"
                aria-pressed={selectedId === profile.id}
                className={`profile-list-item ${selectedId === profile.id ? "current" : ""}`}
                onClick={() => setSelectedId(profile.id)}
              >
                <strong>{profile.id}</strong>
                <span>{profile.mutable ? "Custom" : "Built in"}</span>
              </button>
            ))}
          </div>
        )}
        <Button
          variant="link"
          className="profile-manage-link"
          onClick={onManage}
        >
          Manage profiles
        </Button>
        {error && (
          <p className="profile-error" role="alert">
            {error}
          </p>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button
            disabled={
              busy ||
              (securitySource === "profile" && !selectedId) ||
              (securitySource === "runtime-config" && !runtimeConfig?.available)
            }
            onClick={() => void assign()}
          >
            Save security source
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
