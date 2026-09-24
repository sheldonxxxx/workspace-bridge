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

type Profile = { id: string; revision: string; mutable: boolean };

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
  const [selectedId, setSelectedId] = useState(
    workspace.runtime_grants?.[runtime]?.profile?.id || "",
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let live = true;
    api<{ profiles: Profile[] }>(`/api/runtimes/${runtime}/profiles`)
      .then((data) => {
        if (!live) return;
        setProfiles(data.profiles || []);
        setSelectedId((current) =>
          data.profiles?.some((profile) => profile.id === current)
            ? current
            : data.profiles?.[0]?.id || "",
        );
      })
      .catch((failure) => {
        if (live) setError((failure as Error).message);
      });
    return () => {
      live = false;
    };
  }, [runtime]);

  async function assign() {
    if (!selectedId) return;
    setBusy(true);
    setError("");
    try {
      await api(`/api/workspaces/${workspace.id}/runtimes/${runtime}`, "POST", {
        enabled: workspace.runtime_grants?.[runtime]?.enabled || false,
        profile_id: selectedId,
      });
      await onChanged();
      notify(`Profile ${selectedId} assigned to ${workspace.name}.`);
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
            conversations use the selected profile.
          </DialogDescription>
        </DialogHeader>
        <div
          className="profile-assignment-list"
          role="group"
          aria-label="Available profiles"
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
          <Button disabled={!selectedId || busy} onClick={() => void assign()}>
            Assign profile
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
