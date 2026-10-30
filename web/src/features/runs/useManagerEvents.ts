import { useEffect, useRef } from "react";
import { subscribeManager } from "../../lib/manager-socket";

type ManagerEventsOptions = {
  enabled: boolean;
  /** Narrow run hints to one run; state hints are unaffected. */
  runId?: string;
  onRun?: (runId: string) => void;
  onState?: () => void;
  onReset: () => void;
};

/** Hints only: durable content is always fetched from the REST API. */
export function useManagerEvents(options: ManagerEventsOptions) {
  const callbacks = useRef(options);
  useEffect(() => {
    callbacks.current = options;
  }, [options]);
  const { enabled, runId } = options;

  useEffect(() => {
    if (!enabled) return;
    return subscribeManager((hint) => {
      const { onRun, onState, onReset } = callbacks.current;
      if (hint.type === "reset") onReset();
      else if (hint.type === "state.changed") onState?.();
      else if (!runId || hint.run_id === runId) onRun?.(hint.run_id);
    });
  }, [enabled, runId]);
}
