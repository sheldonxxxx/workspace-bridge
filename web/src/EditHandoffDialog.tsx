import { useEffect, useState, type FormEvent } from "react";
import { api, type Handoff } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

const names = ["TASK.md", "CONTEXT.md", "ACCEPTANCE.md"] as const;
type Plan = {
  title: string;
  expected_title: string;
  documents: Record<string, string>;
  expected_documents: Record<string, string>;
};

export function EditHandoffDialog({
  workspaceId,
  handoff,
  onClose,
  onSaved,
}: {
  workspaceId: string;
  handoff: Handoff | null;
  onClose: () => void;
  onSaved: () => Promise<void>;
}) {
  const [plan, setPlan] = useState<Plan | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let active = true;
    if (handoff)
      api<Plan>(`/api/workspaces/${workspaceId}/jobs/${handoff.id}`)
        .then((value) => {
          if (active) setPlan(value);
        })
        .catch((err: Error) => {
          if (active) setError(err.message);
        });
    return () => {
      active = false;
    };
  }, [workspaceId, handoff]);
  async function save(event: FormEvent) {
    event.preventDefault();
    if (!plan || !handoff) return;
    setBusy(true);
    setError("");
    try {
      await api(
        `/api/workspaces/${workspaceId}/jobs/${handoff.id}`,
        "PUT",
        plan,
      );
      await onSaved();
      onClose();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog
      open={Boolean(handoff)}
      onOpenChange={(open) => {
        if (!open && !busy) onClose();
      }}
    >
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle>Edit handoff</DialogTitle>
          <DialogDescription>
            Edit the full plan documents. Manual dispatch is not tracked; do not
            edit a plan an agent is using.
          </DialogDescription>
        </DialogHeader>
        <form className="dialog-form" onSubmit={(event) => void save(event)}>
          {plan && (
            <div className="form-field">
              <label htmlFor="handoff-title">Title</label>
              <Input
                id="handoff-title"
                required
                maxLength={120}
                value={plan.title}
                onChange={(event) =>
                  setPlan({ ...plan, title: event.target.value })
                }
              />
            </div>
          )}
          {plan &&
            names.map((name) => (
              <div className="form-field" key={name}>
                <label htmlFor={`handoff-${name}`}>{name}</label>
                <Textarea
                  id={`handoff-${name}`}
                  rows={7}
                  value={plan.documents[name]}
                  onChange={(event) =>
                    setPlan({
                      ...plan,
                      documents: {
                        ...plan.documents,
                        [name]: event.target.value,
                      },
                    })
                  }
                />
              </div>
            ))}
          {error && (
            <p className="form-error" role="alert">
              {error}
            </p>
          )}
          <DialogFooter>
            <Button type="submit" disabled={!plan || busy}>
              Save changes
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
