/**
 * The Manager's single live connection to the Bridge.
 *
 * The socket carries hints only (a run ID, or "some audited state moved");
 * content is always reread from REST. Every connection starts with a reset
 * from the Bridge, so a reconnect needs no replay. All subscribers share one
 * socket, which closes when the last one leaves.
 */
export type ManagerHint =
  | { type: "reset" }
  | { type: "state.changed" }
  | { type: "run.changed"; run_id: string };

type Listener = (hint: ManagerHint) => void;

const listeners = new Set<Listener>();
const minDelayMs = 1000;
const maxDelayMs = 30_000;
let socket: WebSocket | null = null;
let retry: number | undefined;
let delay = minDelayMs;

function parse(data: unknown): ManagerHint | null {
  if (typeof data !== "string") return null;
  try {
    const hint: unknown = JSON.parse(data);
    if (!hint || typeof hint !== "object" || !("type" in hint)) return null;
    if (hint.type === "reset" || hint.type === "state.changed")
      return { type: hint.type };
    if (
      hint.type === "run.changed" &&
      "run_id" in hint &&
      typeof hint.run_id === "string" &&
      hint.run_id
    )
      return { type: "run.changed", run_id: hint.run_id };
  } catch {
    // A malformed hint cannot supply content or modify state.
  }
  return null;
}

function connect() {
  if (socket || !listeners.size) return;
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  let current: WebSocket;
  try {
    current = new WebSocket(`${scheme}://${location.host}/api/ws`);
  } catch {
    schedule();
    return;
  }
  socket = current;
  current.onmessage = (event) => {
    const hint = parse(event.data);
    if (!hint) return;
    delay = minDelayMs;
    for (const listener of [...listeners]) listener(hint);
  };
  current.onclose = () => {
    if (socket === current) socket = null;
    schedule();
  };
}

function schedule() {
  if (retry !== undefined || socket || !listeners.size) return;
  const wait = delay * (0.75 + Math.random() * 0.5);
  delay = Math.min(delay * 2, maxDelayMs);
  retry = window.setTimeout(() => {
    retry = undefined;
    connect();
  }, wait);
}

export function subscribeManager(listener: Listener): () => void {
  listeners.add(listener);
  connect();
  return () => {
    listeners.delete(listener);
    if (listeners.size) return;
    if (retry !== undefined) window.clearTimeout(retry);
    retry = undefined;
    delay = minDelayMs;
    const open = socket;
    socket = null;
    if (open) {
      open.onclose = null;
      open.onmessage = null;
      open.close();
    }
  };
}
