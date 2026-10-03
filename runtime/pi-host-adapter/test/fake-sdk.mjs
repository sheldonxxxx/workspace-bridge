// In-process fake SDK transport for pi-host-adapter tests.
//
// Replaces the old fake child-process harness (helpers.mjs/fake-pi.mjs).
// FakeSdkSession mirrors the AgentSession surface the adapter uses:
// sessionId/sessionFile/messages/isStreaming/pendingMessageCount,
// modelRuntime.getAvailableSnapshot(), subscribe/emit for agent events,
// prompt() with preflightResult acceptance, setModel(), abort(),
// bindExtensions() capturing the adapter's ExtensionUIContext,
// getAllTools()/setActiveToolsByName(), and dispose().
export class FakeSdkSession {
  constructor({ sessionId, models = [], toolNames = [] } = {}) {
    this._sessionId = sessionId || "ses-fake-1";
    this._sessionFile = `/tmp/fake-sessions/${this._sessionId}.jsonl`;
    this._models = models;
    this._toolNames = toolNames;
    this._listeners = new Set();
    this._messages = [];
    this._isStreaming = false;
    this._isRetrying = false;
    this._pendingMessageCount = 0;
    this.promptCalls = [];
    this.promptImpl = null;
    this.abortCalls = 0;
    this.setModelCalls = [];
    this.disposeCalls = 0;
    this.disposed = false;
    this.boundUiContext = null;
    this.bindMode = null;
    this.activeTools = null;
    this.modelRuntime = {
      getAvailableSnapshot: () => this._models,
    };
  }

  get sessionId() {
    return this._sessionId;
  }

  get sessionFile() {
    return this._sessionFile;
  }

  get messages() {
    return this._messages;
  }

  get isStreaming() {
    return this._isStreaming;
  }

  get isRetrying() {
    return this._isRetrying;
  }

  get pendingMessageCount() {
    return this._pendingMessageCount;
  }

  setMessages(messages) {
    this._messages = messages;
  }

  setStreaming(value) {
    this._isStreaming = Boolean(value);
  }

  setRetrying(value) {
    this._isRetrying = Boolean(value);
  }

  setPendingMessageCount(value) {
    this._pendingMessageCount = value;
  }

  subscribe(listener) {
    this._listeners.add(listener);
    return () => {
      this._listeners.delete(listener);
    };
  }

  emit(event) {
    for (const listener of [...this._listeners]) {
      listener(event);
    }
  }

  async prompt(text, opts = {}) {
    this.promptCalls.push({ text, opts });
    if (typeof this.promptImpl === "function") {
      return this.promptImpl(text, opts);
    }
    if (typeof opts.preflightResult === "function") {
      opts.preflightResult(true);
    }
    return undefined;
  }

  async abort() {
    this.abortCalls += 1;
  }

  async setModel(model) {
    this.setModelCalls.push(model);
  }

  async bindExtensions({ uiContext, mode }) {
    this.boundUiContext = uiContext;
    this.bindMode = mode;
  }

  getAllTools() {
    return this._toolNames.map((name) => ({ name }));
  }

  setActiveToolsByName(names) {
    this.activeTools = [...names];
  }

  dispose() {
    this.disposeCalls += 1;
    this.disposed = true;
  }
}

export function createFakeTransport({ models = [], toolNames = [], sessionIdPrefix = "ses-fake-" } = {}) {
  const created = [];
  const sessions = [];
  let n = 0;
  const fixedIds = [];

  async function createSdkSession(options) {
    n += 1;
    const sessionId = fixedIds.length ? fixedIds.shift() : `${sessionIdPrefix}${n}`;
    const session = new FakeSdkSession({ sessionId, models, toolNames });
    created.push({ ...options, session });
    sessions.push(session);
    // Mirror the real transport contract: the UI context is bound to the
    // session before any event can suspend on it, and the adapter's
    // audit/permission listener subscribes to agent events.
    if (options && options.uiContext) {
      await session.bindExtensions({ uiContext: options.uiContext, mode: "rpc" });
    }
    if (options && typeof options.onEvent === "function") {
      session.subscribe((event) => {
        try {
          options.onEvent(event);
        } catch {
          // Listener errors never break the session.
        }
      });
    }
    // Mirror the real transport's sessionManager identity so WBRP
    // conversation creation (which requires a sessionFile) works.
    const sessionManager = {
      getSessionFile: () => session.sessionFile,
    };
    return { session, sessionManager };
  }

  async function listModelsFn() {
    return models;
  }

  return {
    created,
    sessions,
    fixedIds,
    createSdkSession,
    listModelsFn,
    lastCreated() {
      return created[created.length - 1];
    },
    lastSession() {
      return sessions[sessions.length - 1];
    },
  };
}

// Drive one permission ask through the adapter-bound UI context: emit the
// tool start event, then call select() with the exact trusted marker and
// options. Returns the suspended select promise plus the pending record.
export async function askPermission(adapter, directory, sessionId, { toolCallId, toolName, args, options }) {
  const entry = adapter.sessions.get(sessionId);
  if (!entry) throw new Error("session entry is missing");
  entry.session.emit({ type: "tool_execution_start", toolCallId, toolName, args });
  await new Promise((resolve) => setImmediate(resolve));
  const selectPromise = entry.session.boundUiContext.select(
    `WB_PERMISSION_V1:${toolCallId}`, options);
  // select() is synchronous up to the suspension point, but await one
  // macrotask so the pending record is always visible afterwards.
  await new Promise((resolve) => setImmediate(resolve));
  const listed = await adapter.listPermissions(directory, sessionId);
  return { selectPromise, listed, entry };
}

// Track settlement of a select promise without hanging the test.
export function trackSelect(promise) {
  const state = { settled: false, value: "unsettled" };
  promise.then(
    (value) => {
      state.settled = true;
      state.value = value;
    },
    (error) => {
      state.settled = true;
      state.value = `rejected:${String((error && error.message) || error)}`;
    },
  );
  return state;
}
