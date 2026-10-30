import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { api } from "@/lib/api";
import type { ConfirmState } from "@/components/manager-types";
import { getCheckpointStats, type CheckpointStats } from "./api";

function size(bytes: number) {
  if (bytes < 1024 * 1024) return `${Math.ceil(bytes / 1024)} KiB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MiB`;
}

export function WorkspaceCheckpoints({
  workspaceId,
  onConfirm,
  onNotice,
}: {
  workspaceId: string;
  onConfirm: (state: ConfirmState) => void;
  onNotice: (message: string) => void;
}) {
  const [stats, setStats] = useState<CheckpointStats | null>(null);
  const [error, setError] = useState("");
  const [version, setVersion] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    getCheckpointStats(workspaceId, controller.signal).then(
      (value) => {
        setStats(value);
        setError("");
      },
      (failure: unknown) => {
        if (!controller.signal.aborted)
          setError(
            failure instanceof Error ? failure.message : String(failure),
          );
      },
    );
    return () => controller.abort();
  }, [workspaceId, version]);

  return (
    <div className="settings-field">
      <Label>Run checkpoints</Label>
      <p>
        The Node keeps a copy of files before and after each run so run diffs
        stay readable. Kept for 14 days and up to 1 GiB per workspace.
      </p>
      <p>
        {error
          ? `Storage usage unavailable: ${error}`
          : stats
            ? `${stats.checkpoints} checkpoints · ${size(stats.store_bytes)}`
            : "Loading storage usage…"}
      </p>
      <Button
        variant="outline"
        size="sm"
        disabled={!stats || stats.checkpoints === 0}
        onClick={() =>
          onConfirm({
            title: "Clear run checkpoints?",
            description:
              "Every stored checkpoint for this workspace is deleted from the Node. Run diffs for past runs stop working; workspace files are not changed.",
            destructive: true,
            action: async () => {
              await api(`/api/workspaces/${workspaceId}/checkpoints`, "DELETE");
              onNotice("Run checkpoints cleared.");
              setVersion((value) => value + 1);
            },
          })
        }
      >
        Clear checkpoints
      </Button>
    </div>
  );
}
