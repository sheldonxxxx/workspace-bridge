import { useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { api, dateTime, type Interaction, type Run } from "@/lib/api";
import type { ConfirmState } from "@/components/manager-types";

const newRequestId = () => crypto.randomUUID().replace(/-/g, "").slice(0, 24);

type Handlers = {
  onConfirm: (state: ConfirmState) => void;
  onNotice: (message: string) => void;
  onChanged: () => Promise<void>;
};

export function ChatGptSeen({ lastCall }: { lastCall?: string | null }) {
  return (
    <p className="muted-note">
      {lastCall
        ? `ChatGPT last called the Bridge ${dateTime(lastCall)}.`
        : "ChatGPT has not called the Bridge since it started."}{" "}
      ChatGPT normally runs this; use these controls when it cannot.
    </p>
  );
}

/** Answer a waiting request by hand; the Bridge records the answer as `manager`. */
export function ManualAnswer({
  runId,
  item,
  onConfirm,
  onNotice,
  onChanged,
}: { runId: string; item: Interaction } & Handlers) {
  const [reason, setReason] = useState("");
  const [fields, setFields] = useState<Record<string, string>>({});
  const title = item.details?.title || item.kind;

  const send = async (body: Record<string, unknown>) => {
    await api(`/api/runs/${runId}/answer`, "POST", {
      interaction_id: item.id,
      reason: reason.trim(),
      ...body,
    });
    onNotice("Answered in the Manager.");
    await onChanged();
  };

  const choose = (id: string, label: string) =>
    onConfirm({
      title: `Answer “${title}” with “${label}”?`,
      description:
        "This answers the agent's request now and is recorded as decided in the Manager.",
      destructive: true,
      action: () => send({ choice_id: id }),
    });

  const formFields = item.details?.fields || [];
  const submitForm = (event: FormEvent) => {
    event.preventDefault();
    const answers = Object.fromEntries(
      formFields.map((field) => [
        field.id,
        { answers: [fields[field.id] ?? ""] },
      ]),
    );
    onConfirm({
      title: `Submit answers to “${title}”?`,
      description:
        "This answers the agent's request now and is recorded as decided in the Manager.",
      destructive: true,
      action: () => send({ answers }),
    });
  };

  return (
    <details className="manual-control">
      <summary>Answer by hand</summary>
      <div className="form-field">
        <Label htmlFor={`reason-${item.id}`}>Reason (optional)</Label>
        <Input
          id={`reason-${item.id}`}
          value={reason}
          maxLength={500}
          placeholder="Answered in the Manager"
          onChange={(event) => setReason(event.target.value)}
        />
      </div>
      {item.kind === "form" ? (
        <form className="dialog-form" onSubmit={submitForm}>
          {formFields.map((field) => (
            <div className="form-field" key={field.id}>
              <Label htmlFor={`${item.id}-${field.id}`}>
                {field.question || field.header || field.id}
              </Label>
              {field.options?.length ? (
                <select
                  id={`${item.id}-${field.id}`}
                  className="native-select"
                  required
                  value={fields[field.id] ?? ""}
                  onChange={(event) =>
                    setFields({ ...fields, [field.id]: event.target.value })
                  }
                >
                  <option value="">Choose</option>
                  {field.options.map((option) => (
                    <option key={option.label} value={option.label}>
                      {option.label}
                    </option>
                  ))}
                </select>
              ) : (
                <Input
                  id={`${item.id}-${field.id}`}
                  required
                  value={fields[field.id] ?? ""}
                  onChange={(event) =>
                    setFields({ ...fields, [field.id]: event.target.value })
                  }
                />
              )}
            </div>
          ))}
          <Button type="submit" size="sm">
            Submit answers
          </Button>
        </form>
      ) : (
        <div className="inspector-actions">
          {(item.details?.choices || []).map((choice) => (
            <Button
              key={choice.id}
              type="button"
              variant="outline"
              size="sm"
              onClick={() => choose(choice.id, choice.label)}
            >
              {choice.label}
            </Button>
          ))}
        </div>
      )}
    </details>
  );
}

/** Steer or queue an instruction by hand; receipts show it came from the Manager. */
export function ManualInput({
  run,
  lastCall,
  onNotice,
  onChanged,
}: {
  run: Run;
  lastCall?: string | null;
  onNotice: (message: string) => void;
  onChanged: () => Promise<void>;
}) {
  const [instruction, setInstruction] = useState("");
  const [mode, setMode] = useState<"steer" | "queue">("steer");
  const [requestId, setRequestId] = useState(newRequestId);
  const [busy, setBusy] = useState(false);
  const capabilities = run.input_capabilities;
  const supported = !capabilities || capabilities[mode];

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    try {
      await api(`/api/runs/${run.run_id}/input`, "POST", {
        instruction,
        mode,
        request_id: requestId,
      });
      setInstruction("");
      setRequestId(newRequestId());
      onNotice(mode === "steer" ? "Correction sent." : "Follow-up queued.");
      await onChanged();
    } catch (error) {
      onNotice(error instanceof Error ? error.message : "Could not send.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <details className="manual-control">
      <summary>Manual control</summary>
      <ChatGptSeen lastCall={lastCall} />
      <form className="dialog-form" onSubmit={submit}>
        <div className="form-field">
          <Label htmlFor="manual-mode">Delivery</Label>
          <select
            id="manual-mode"
            className="native-select"
            value={mode}
            onChange={(event) =>
              setMode(event.target.value as "steer" | "queue")
            }
          >
            <option value="steer">Steer now</option>
            <option value="queue">Queue after the current response</option>
          </select>
        </div>
        <div className="form-field">
          <Label htmlFor="manual-instruction">Instruction</Label>
          <Textarea
            id="manual-instruction"
            required
            rows={3}
            maxLength={8000}
            value={instruction}
            onChange={(event) => setInstruction(event.target.value)}
          />
        </div>
        {!supported && (
          <p className="muted-note">This agent does not support {mode}.</p>
        )}
        <Button type="submit" size="sm" disabled={busy || !supported}>
          Send instruction
        </Button>
      </form>
    </details>
  );
}
