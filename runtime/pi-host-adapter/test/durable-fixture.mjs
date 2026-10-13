// Provider-free SDK fixture for process-crash tests. The real adapter,
// trusted permission hook, durable store and WBRP facade are exercised.
import fs from "node:fs";
import path from "node:path";
import { PiAdapter } from "../adapter.mjs";
import { canonicalizeProjectsDir } from "../paths.mjs";
import { createFakeTransport } from "./fake-sdk.mjs";
import { createTrustedPermissionExtension } from "../trusted-permission-extension.mjs";

export function durableFixture(root) {
  const projectsRoot = path.join(root, "projects");
  const directory = path.join(projectsRoot, "work");
  const agentDir = path.join(root, "agent");
  fs.mkdirSync(directory, { recursive: true });
  fs.mkdirSync(agentDir, { recursive: true, mode: 0o700 });
  const transport = createFakeTransport();
  const logs = [];
  const adapter = new PiAdapter({
    projectsRoot: canonicalizeProjectsDir(projectsRoot), agentDir,
    createSessionFn: async (options) => {
      const created = await transport.createSdkSession(options);
      created.session._sessionFile = path.join(agentDir, `${created.session.sessionId}.jsonl`);
      if (!fs.existsSync(created.session.sessionFile)) {
        fs.writeFileSync(created.session.sessionFile, "owned-test-session\n", { mode: 0o600 });
      }
      created.session.promptImpl = (_text, opts) => {
        created.session.setStreaming(true);
        opts.preflightResult(true);
        return new Promise(() => {});
      };
      return created;
    },
    listModelsFn: transport.listModelsFn,
    onLog: (...record) => logs.push(record),
  });
  return { adapter, transport, directory: fs.realpathSync(directory), agentDir, logs };
}

export function boundToolHook(transport) {
  const options = transport.lastCreated();
  let handler;
  createTrustedPermissionExtension({ policy: options.policy, cwd: options.cwd,
    onToolIntent: options.onToolIntent })({ on: (type, fn) => {
    if (type === "tool_call") handler = fn;
  } });
  return (event) => handler(event, { ui: transport.lastSession().boundUiContext });
}

export const flush = () => new Promise((resolve) => setImmediate(resolve));
