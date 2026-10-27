import { useEffect, useState, type FormEvent } from "react";
import {
  api,
  runtimeName,
  type Handoff,
  type Run,
  type Workspace,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

const newRequestId = () => crypto.randomUUID().replace(/-/g, "").slice(0, 24);

/** Start a run by hand through the same route checks ChatGPT's start_run uses. */
export function StartRunDialog({
  workspaces,
  onClose,
  onStarted,
}: {
  workspaces: Workspace[];
  onClose: () => void;
  onStarted: (run: Run) => void;
}) {
  const usable = workspaces.filter((workspace) => workspace.enabled);
  const [workspaceId, setWorkspaceId] = useState(usable[0]?.id || "");
  const [adapterId, setAdapterId] = useState("");
  const [source, setSource] = useState<"handoff" | "instruction">("handoff");
  const [jobId, setJobId] = useState("");
  const [instruction, setInstruction] = useState("");
  const [isolation, setIsolation] = useState<"none" | "worktree">("none");
  const [handoffs, setHandoffs] = useState<Handoff[]>([]);
  const [requestId] = useState(newRequestId);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const workspace = usable.find((item) => item.id === workspaceId);
  const routes = Object.values(workspace?.routes || {}).filter(
    (route) => route.enabled && route.adapter_enabled,
  );

  useEffect(() => {
    if (!workspaceId) return;
    let current = true;
    api<{ handoffs: Handoff[] }>(`/api/workspaces/${workspaceId}/jobs`)
      .then((result) => {
        if (!current) return;
        const prepared = (result.handoffs || []).filter(
          (item) => item.state === "prepared",
        );
        setHandoffs(prepared);
        setJobId((previous) =>
          prepared.some((item) => item.id === previous)
            ? previous
            : prepared[0]?.id || "",
        );
      })
      .catch((failure: Error) => {
        if (current) setError(failure.message);
      });
    return () => {
      current = false;
    };
  }, [workspaceId]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const run = await api<Run>(
        `/api/workspaces/${workspaceId}/runs`,
        "POST",
        {
          adapter_id: adapterId || routes[0]?.adapter_id,
          request_id: requestId,
          isolation,
          ...(source === "handoff"
            ? { job_id: jobId }
            : { instruction: instruction.trim() }),
        },
      );
      onStarted(run);
    } catch (failure) {
      setError((failure as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const ready =
    Boolean(workspace && routes.length) &&
    (source === "handoff" ? Boolean(jobId) : Boolean(instruction.trim()));

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Start a run by hand</DialogTitle>
          <DialogDescription>
            ChatGPT normally starts runs. This uses the same route and security
            checks, and is recorded as started in the Manager.
          </DialogDescription>
        </DialogHeader>
        <form className="dialog-form" onSubmit={submit}>
          <div className="form-field">
            <Label htmlFor="start-workspace">Workspace</Label>
            <select
              id="start-workspace"
              className="native-select"
              value={workspaceId}
              onChange={(event) => {
                setWorkspaceId(event.target.value);
                setAdapterId("");
              }}
            >
              {usable.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
          </div>
          <div className="form-field">
            <Label htmlFor="start-agent">Agent</Label>
            <select
              id="start-agent"
              className="native-select"
              value={adapterId || routes[0]?.adapter_id || ""}
              onChange={(event) => setAdapterId(event.target.value)}
            >
              {routes.map((route) => (
                <option key={route.adapter_id} value={route.adapter_id}>
                  {route.name} · {runtimeName(route.runtime_type)}
                </option>
              ))}
            </select>
            {!routes.length && (
              <small>This workspace has no enabled agent route.</small>
            )}
          </div>
          <div className="form-field">
            <Label htmlFor="start-source">Start from</Label>
            <select
              id="start-source"
              className="native-select"
              value={source}
              onChange={(event) =>
                setSource(event.target.value as "handoff" | "instruction")
              }
            >
              <option value="handoff">A prepared handoff</option>
              <option value="instruction">A direct instruction</option>
            </select>
          </div>
          {source === "handoff" ? (
            <div className="form-field">
              <Label htmlFor="start-handoff">Handoff</Label>
              <select
                id="start-handoff"
                className="native-select"
                value={jobId}
                onChange={(event) => setJobId(event.target.value)}
              >
                {handoffs.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.title}
                  </option>
                ))}
              </select>
              {!handoffs.length && <small>No prepared handoffs.</small>}
            </div>
          ) : (
            <div className="form-field">
              <Label htmlFor="start-instruction">Instruction</Label>
              <Textarea
                id="start-instruction"
                rows={4}
                maxLength={8000}
                value={instruction}
                onChange={(event) => setInstruction(event.target.value)}
              />
            </div>
          )}
          <div className="form-field">
            <Label htmlFor="start-isolation">Where it works</Label>
            <select
              id="start-isolation"
              className="native-select"
              value={isolation}
              onChange={(event) =>
                setIsolation(event.target.value as "none" | "worktree")
              }
            >
              <option value="none">In the workspace</option>
              <option value="worktree">In an isolated Git worktree</option>
            </select>
          </div>
          {error && <p role="alert">{error}</p>}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={busy || !ready}>
              Start run
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
