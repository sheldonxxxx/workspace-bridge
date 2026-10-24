import { useEffect, useRef, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { dateTime, type RunCheckpoint } from "@/lib/api";
import { getRunDiff } from "./api";

const statusText: Record<RunCheckpoint["status"], string> = {
  complete: "Captured before and after the run.",
  partial: "Some files were not captured; they are marked below.",
  pending: "Checkpoints are still being captured.",
  unavailable: "Checkpoints are unavailable for this run.",
  expired: "Checkpoints were removed by retention.",
};
const tone: Record<RunCheckpoint["status"], string> = {
  complete: "success",
  partial: "warning",
  pending: "warning",
  unavailable: "danger",
  expired: "neutral",
};

export function RunChanges({
  runId,
  checkpoint,
}: {
  runId: string;
  checkpoint: RunCheckpoint;
}) {
  const [selected, setSelected] = useState<string | null | undefined>();
  const [patch, setPatch] = useState("");
  const [nextOffset, setNextOffset] = useState<number | null>(null);
  const [redacted, setRedacted] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const request = useRef<AbortController | null>(null);
  const readable =
    checkpoint.status === "complete" || checkpoint.status === "partial";
  const errorCode = checkpoint.pre.error || checkpoint.post.error;
  const changes = checkpoint.changes || [];

  // The inspector keys this panel by run ID, so state never crosses runs.
  useEffect(() => () => request.current?.abort(), []);

  async function load(path: string | null, offset: number) {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    setError("");
    if (offset === 0) {
      setSelected(path);
      setPatch("");
    }
    try {
      const page = await getRunDiff(runId, path, offset, controller.signal);
      setPatch((current) => (offset === 0 ? page.patch : current + page.patch));
      setNextOffset(page.next_offset);
      setRedacted((current) =>
        offset === 0 ? page.redacted : current || page.redacted,
      );
    } catch (failure) {
      if (!controller.signal.aborted)
        setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      if (request.current === controller) setLoading(false);
    }
  }

  return (
    <section className="request-panel run-changes-panel">
      <Badge
        variant="outline"
        className={`state-badge tone-${tone[checkpoint.status]}`}
      >
        {checkpoint.status}
      </Badge>
      <h3>Changes during this run</h3>
      <p>
        {statusText[checkpoint.status]}
        {errorCode ? ` Reason: ${errorCode.replaceAll("_", " ")}.` : ""}
      </p>
      <p>
        Before: {dateTime(checkpoint.pre.captured_at ?? undefined)} · After:{" "}
        {dateTime(checkpoint.post.captured_at ?? undefined)}
      </p>
      {checkpoint.late && (
        <p>
          Captured more than a minute after the run ended; later edits may be
          included.
        </p>
      )}
      {checkpoint.overlapping_runs.length > 0 && (
        <p>
          Other runs in this workspace overlapped this one, so their edits may
          appear here: {checkpoint.overlapping_runs.join(", ")}
        </p>
      )}
      {readable && (
        <>
          <p>
            {checkpoint.changed_paths ?? changes.length} changed{" "}
            {(checkpoint.changed_paths ?? changes.length) === 1
              ? "path"
              : "paths"}
            {changes.length < (checkpoint.changed_paths ?? 0)
              ? `, first ${changes.length} listed`
              : ""}
            . Changes in this window are not proof of who made them.
          </p>
          <ul className="run-changes-list">
            {changes.map((change) => (
              <li key={change.path}>
                <button
                  type="button"
                  className={selected === change.path ? "selected" : ""}
                  onClick={() => load(change.path, 0)}
                >
                  <span className={`change-${change.status}`}>
                    {change.status}
                  </span>
                  <span className="change-path">{change.path}</span>
                  {change.not_captured && (
                    <span className="change-note">
                      not captured: {change.not_captured.replaceAll("_", " ")}
                    </span>
                  )}
                </button>
              </li>
            ))}
          </ul>
          <Button
            variant="outline"
            size="sm"
            disabled={loading}
            onClick={() => load(null, 0)}
          >
            Show full diff
          </Button>
        </>
      )}
      {error && <p className="form-error">{error}</p>}
      {selected !== undefined && patch && (
        <>
          {redacted && <p>Secret-like text in this diff was redacted.</p>}
          <pre className="run-diff">{patch}</pre>
          {nextOffset !== null && (
            <Button
              variant="outline"
              size="sm"
              disabled={loading}
              onClick={() => load(selected, nextOffset)}
            >
              Load more
            </Button>
          )}
        </>
      )}
      {selected !== undefined && !patch && !loading && !error && (
        <p>No textual changes for this selection.</p>
      )}
    </section>
  );
}
