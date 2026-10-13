/** Serialize snapshot reads and collapse a burst into one pending refresh. */
export function createRefreshQueue(read: () => Promise<void>) {
  let disposed = false;
  let pending = false;
  let running: Promise<void> | null = null;
  let timer: number | undefined;

  function refresh(): Promise<void> {
    if (disposed) return Promise.resolve();
    pending = true;
    if (timer !== undefined) {
      window.clearTimeout(timer);
      timer = undefined;
    }
    if (running) return running;
    running = (async () => {
      try {
        while (pending && !disposed) {
          pending = false;
          await read();
        }
      } finally {
        running = null;
      }
    })();
    return running;
  }

  function schedule() {
    if (disposed) return;
    pending = true;
    if (running || timer !== undefined) return;
    timer = window.setTimeout(() => {
      timer = undefined;
      void refresh().catch(() => {
        // Callers retain their last snapshot when a background read fails.
      });
    }, 100);
  }

  return {
    refresh,
    schedule,
    dispose() {
      disposed = true;
      pending = false;
      if (timer !== undefined) window.clearTimeout(timer);
    },
  };
}
