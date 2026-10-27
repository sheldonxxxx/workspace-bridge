import { useEffect, useState } from "react";
import { api, type Workspace } from "@/lib/api";
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
  builtin: boolean;
  description?: string;
  restricts: string[];
};

type AdapterSecurity = { enforces: string[]; native: boolean };

// Rules a profile restricts that this agent cannot enforce; the Bridge
// refuses such a route, so the dialog shows why before saving.
function unenforced(profile: Profile, security: AdapterSecurity | null) {
  if (!security) return [];
  return profile.restricts.filter((key) => !security.enforces.includes(key));
}

export function ProfileAssignment({
  workspace,
  adapterId,
  onClose,
  onManage,
  onChanged,
  notify,
}: {
  workspace: Workspace;
  adapterId: string;
  onClose: () => void;
  onManage: () => void;
  onChanged: () => Promise<void>;
  notify: (message: string) => void;
}) {
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [security, setSecurity] = useState<AdapterSecurity | null>(null);
  const route = workspace.routes?.[adapterId];
  const [securitySource, setSecuritySource] = useState<"profile" | "native">(
    route?.security_binding?.source || "profile",
  );
  const [selectedId, setSelectedId] = useState(route?.profile?.id || "");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let live = true;
    Promise.all([
      api<{ profiles: Profile[] }>("/api/profiles"),
      api<{ security?: AdapterSecurity }>(`/api/adapters/${adapterId}`),
    ])
      .then(([data, adapter]) => {
        if (!live) return;
        const caps = adapter.security || null;
        const usable = (data.profiles || []).filter(
          (profile) => unenforced(profile, caps).length === 0,
        );
        setProfiles(data.profiles || []);
        setSecurity(caps);
        setSelectedId((current) =>
          usable.some((profile) => profile.id === current)
            ? current
            : usable[0]?.id || "",
        );
      })
      .catch((failure) => {
        if (live) setError((failure as Error).message);
      })
      .finally(() => {
        if (live) setLoading(false);
      });
    return () => {
      live = false;
    };
  }, [adapterId]);

  async function assign() {
    if (securitySource === "profile" && !selectedId) return;
    setBusy(true);
    setError("");
    try {
      await api(`/api/workspaces/${workspace.id}/routes/${adapterId}`, "POST", {
        enabled: route?.enabled || false,
        security_source: securitySource,
        profile_id: securitySource === "profile" ? selectedId : null,
      });
      await onChanged();
      notify(
        securitySource === "profile"
          ? `Profile ${selectedId} assigned to ${workspace.name}.`
          : `${route?.name || "The agent"} will use its own security in ${workspace.name}.`,
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
          <DialogTitle>Route security</DialogTitle>
          <DialogDescription>
            Choose the security {route?.name || "this agent"} runs under in{" "}
            {workspace.name}. Every run uses the profile's current rules.
          </DialogDescription>
        </DialogHeader>
        {security?.native && (
          <fieldset className="profile-assignment-modes">
            <legend>Security source</legend>
            <label className="profile-assignment-mode">
              <input
                type="radio"
                name="security-source"
                value="native"
                checked={securitySource === "native"}
                onChange={() => setSecuritySource("native")}
              />
              <span>
                <strong>Use the agent's own security</strong>
                <small>
                  Follow the agent's native configuration; each native approval
                  becomes an interaction.
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
                <strong>Use a Workspace Bridge profile</strong>
                <small>The agent enforces the profile's rules.</small>
              </span>
            </label>
          </fieldset>
        )}
        {securitySource === "profile" && (
          <div
            className="profile-assignment-list"
            role="group"
            aria-label="Workspace Bridge profiles"
          >
            {loading && <p role="status">Loading profiles…</p>}
            {!loading && profiles.length > 0 && !selectedId && (
              <p role="status">
                This agent cannot enforce any profile
                {security?.native && "; use its own security instead"}.
              </p>
            )}
            {profiles.map((profile) => {
              const missing = unenforced(profile, security);
              return (
                <button
                  key={profile.id}
                  type="button"
                  aria-pressed={selectedId === profile.id}
                  className={`profile-list-item ${selectedId === profile.id ? "current" : ""}`}
                  disabled={missing.length > 0}
                  onClick={() => setSelectedId(profile.id)}
                >
                  <strong>{profile.id}</strong>
                  <span>
                    {profile.builtin ? "Built in" : "Custom"}
                    {missing.length > 0 &&
                      ` · This agent cannot enforce ${missing.join(", ")}`}
                  </span>
                </button>
              );
            })}
          </div>
        )}
        <Button
          variant="link"
          className="profile-manage-link"
          onClick={onManage}
        >
          Manage adapters
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
            disabled={busy || (securitySource === "profile" && !selectedId)}
            onClick={() => void assign()}
          >
            Save security source
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
