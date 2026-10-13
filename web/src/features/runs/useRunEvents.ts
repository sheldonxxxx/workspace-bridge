import { useEffect, useRef } from "react";

type RunEventsOptions = {
  enabled: boolean;
  runId?: string;
  onChange: (runId: string) => void;
  onReset: () => void;
};

/** Hints only: durable run content is always fetched from the REST API. */
export function useRunEvents(options: RunEventsOptions) {
  const callbacks = useRef(options);
  useEffect(() => {
    callbacks.current = options;
  }, [options]);
  const { enabled, runId } = options;

  useEffect(() => {
    if (!enabled) return;
    let source: EventSource;
    let closed = false;
    try {
      const query = runId ? `?${new URLSearchParams({ run_id: runId })}` : "";
      source = new EventSource(`/api/run-events${query}`);
    } catch {
      return;
    }
    const changed = (event: MessageEvent) => {
      if (closed) return;
      try {
        const hint: unknown = JSON.parse(event.data);
        if (!hint || typeof hint !== "object" || !("run_id" in hint)) return;
        const id = hint.run_id;
        if (typeof id !== "string" || !id || (runId && id !== runId)) return;
        callbacks.current.onChange(id);
      } catch {
        // A malformed hint cannot supply content or modify run state.
      }
    };
    const reset = () => {
      if (!closed) callbacks.current.onReset();
    };
    source.addEventListener("run.changed", changed as EventListener);
    source.addEventListener("reset", reset);
    return () => {
      closed = true;
      source.removeEventListener("run.changed", changed as EventListener);
      source.removeEventListener("reset", reset);
      source.close();
    };
  }, [enabled, runId]);
}
