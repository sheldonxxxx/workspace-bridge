// Private adapter process for Workspace Bridge.
//
// This process is a client only: it connects to an externally managed native
// OpenCode server and exposes a narrow, token-authenticated protocol on the
// private Compose network. It has no host-published port, no arbitrary command
// endpoint, and never starts an OpenCode server. When WB_RUNTIME_TOKEN is absent
// every operational endpoint is locked; the bridge fails closed until it is set.
import { randomUUID } from "node:crypto";

import { buildClient, buildPermissionClient, SdkRuntime, EventHub, ADAPTER_VERSION } from "./sdk-runtime.mjs";
import { initFromEnv, emit as oplogEmit } from "./oplog.mjs";
import { createAdapterServer } from "./server.mjs";

initFromEnv(process.env);

const PORT = Number(process.env.WB_ADAPTER_PORT || 8770);
const HOST = process.env.WB_ADAPTER_HOST || "0.0.0.0";
const SERVER_URL = (process.env.WB_OPENCODE_SERVER_URL || "").trim().replace(/\/+$/, "");
const USERNAME = process.env.WB_OPENCODE_SERVER_USERNAME || "";
const PASSWORD = process.env.WB_OPENCODE_SERVER_PASSWORD || "";
const TOKEN = process.env.WB_RUNTIME_TOKEN || "";

const serverConfigured = Boolean(SERVER_URL);
const INSTANCE = randomUUID();
let runtime = null;
let hub = null;
if (serverConfigured) {
  const client = buildClient({ baseUrl: SERVER_URL, username: USERNAME, password: PASSWORD });
  const permissionClient = buildPermissionClient({
    baseUrl: SERVER_URL, username: USERNAME, password: PASSWORD,
  });
  runtime = new SdkRuntime({ client, permissionClient });
  hub = new EventHub({ client, onLog: oplogEmit });
  hub.start();
}

const server = createAdapterServer({
  runtime,
  hub,
  token: TOKEN,
  serverConfigured,
  instance: INSTANCE,
  adapterVersion: ADAPTER_VERSION,
  onLog: oplogEmit,
});

server.listen(PORT, HOST, () => {
  // Structured startup record: version + booleans only, never the server
  // URL, username, password or token.
  oplogEmit("INFO", "adapter", "adapter_ready", {
    adapter_version: ADAPTER_VERSION,
    server_configured: serverConfigured,
    locked: !TOKEN,
  });
});

function shutdown() {
  if (hub) hub.stop();
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 3000).unref();
}
process.on("SIGTERM", shutdown);
process.on("SIGINT", shutdown);
