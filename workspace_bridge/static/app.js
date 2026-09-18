"use strict";
// All untrusted names, paths, reports, and server responses use textContent.
// Tokens stay in memory; no localStorage, sessionStorage, cookies, or third-party assets.
let adminToken = "";
let currentWorkspace = null;
let config = {};
let bridgeState = {};
const $ = (id) => document.getElementById(id);
function node(tag, text, cls) { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n; }
function message(text) { $("message").textContent = text; $("message").style.display = "block"; setTimeout(() => { $("message").style.display = "none"; }, 6000); }
async function api(path, method = "GET", body) {
  const r = await fetch(path, {method, credentials: "omit", headers: {"Authorization": `Bearer ${adminToken}`, ...(body ? {"Content-Type": "application/json"} : {})}, body: body ? JSON.stringify(body) : undefined});
  const value = await r.json(); if (!r.ok) throw new Error(value.error || `HTTP ${r.status}`); return value;
}
function button(text, fn, cls = "secondary") { const b = node("button", text, cls); b.type = "button"; b.onclick = () => Promise.resolve().then(fn).catch(e => message(e.message)); return b; }
function show(title, value) { $("output-title").textContent = title; $("output").textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2); $("output-panel").hidden = false; $("output-panel").scrollIntoView({behavior: "smooth", block: "center"}); }
function credential(value) { show("Shared bridge credential — shown once", `This credential authorizes ALL enabled workspace mappings. Save it in your local tunnel environment, NOT in ChatGPT.\n\n${value.token}\n\nAfter rotation, update the tunnel environment and restart tunnel-client. The old token is revoked for subsequent calls.`); }
async function manage(id, operation, excludes) {
  const result = await api(`/api/workspaces/${id}`, "POST", {operation, ...(excludes ? {excludes} : {})});
  if (result.token) credential(result); await refresh();
}
function tunnelProfile() {
  const profile = `config_version: 1\ncontrol_plane:\n  tunnel_id: tunnel_REPLACE_WITH_YOUR_32_HEX_ID\n  api_key: env:CONTROL_PLANE_API_KEY\nmcp:\n  server_urls:\n    - channel: main\n      url: http://127.0.0.1:${config.mcp_port}/mcp\n  extra_headers:\n    X-Bridge-Token: env:WORKSPACE_BRIDGE_TOKEN\n  discovery_extra_headers:\n    X-Bridge-Token: env:WORKSPACE_BRIDGE_TOKEN\nhealth:\n  listen_addr: 127.0.0.1:8790\n`;
  show("Tunnel profile (contains no credentials)", profile + "\nSave outside your project as bridge-tunnel.yaml. Supply keys locally, then run:\n\ntunnel-client doctor --config /absolute/path/bridge-tunnel.yaml --explain\ntunnel-client run --config /absolute/path/bridge-tunnel.yaml\n\nUse this ONE tunnel and ONE ChatGPT connection for all enabled mappings. Adding workspaces requires no new tunnel. Never tunnel the management listener.");
}
async function jobs(ws) {
  currentWorkspace = ws;
  const data = await api(`/api/workspaces/${ws.id}/jobs`);
  $("jobs-title").textContent = `${ws.name} · Handoffs`; $("jobs-panel").hidden = false; $("jobs").replaceChildren();
  if (!data.handoffs.length) $("jobs").append(node("p", "No handoffs yet. Ask ChatGPT to inspect the project and prepare a handoff.", "muted"));
  for (const j of data.handoffs) {
    const row = node("div", undefined, "job"); row.append(node("h3", `${j.title} · ${j.legacy_handoff ? "legacy" : j.state === "prepared" ? "published" : j.state}`), node("div", j.path, "path"));
    const actions = node("div", undefined, "actions");
    actions.append(button("Copy OpenCode prompt", async () => { try { await navigator.clipboard.writeText(j.copy_prompt); message("Handoff prompt copied. Paste it into OpenCode yourself."); } catch { show("Copy this prompt", j.copy_prompt); } }));
    for (const [label, doc] of [["Plan", "TASK.md"], ["Context", "CONTEXT.md"], ["Acceptance", "ACCEPTANCE.md"]]) {
      actions.append(button(label, async () => { const result = await api(`/api/workspaces/${ws.id}/document?${new URLSearchParams({job_id:j.id, document:doc})}`); show(`${j.title} · ${label}`, result); }));
    }
    row.append(actions); $("jobs").append(row);
  }
  if (data.next_offset !== null) $("jobs").append(node("p", "Showing the most recent 40 handoffs. Use the paginated MCP tool for older entries.", "muted"));
}
async function refresh() {
  const [data, history, state] = await Promise.all([api("/api/workspaces"), api("/api/events"), api("/api/bridge")]);
  bridgeState = state;
  $("bridge-status").textContent = !state.configured ? "Not configured" : state.enabled ? "Enabled" : "Paused";
  $("bridge-pause").textContent = state.enabled ? "Pause all MCP access" : "Enable MCP access";
  $("bridge-pause").disabled = !state.configured;
  $("bridge-rotate").textContent = state.configured ? "Rotate bridge token" : "Create bridge token";
  $("shared-endpoint").textContent = `http://127.0.0.1:${config.mcp_port}/mcp`;
  $("workspace-count").textContent = data.workspaces.length; $("workspaces").replaceChildren();
  if (!data.workspaces.length) $("workspaces").append(node("p", "No projects are exposed. Add a workspace below.", "muted"));
  for (const ws of data.workspaces) {
    const row = node("div", undefined, "workspace"); const title = node("h3", ws.name); title.append(node("span", ws.enabled ? "Enabled" : "Disabled", ws.enabled ? "enabled" : "disabled"));
    row.append(title, node("div", ws.root, "path"), node("div", `Workspace ID: ${ws.id} · Write scope: ${ws.write_scope}`, "path"));
    const actions = node("div", undefined, "actions");
    actions.append(button(ws.enabled ? "Disable access" : "Enable access", () => { if (ws.enabled || confirm("Expose this mapping to every chat using the shared bridge connection?")) return manage(ws.id, ws.enabled ? "disable" : "enable"); }, ws.enabled ? "danger" : "secondary"), button("Handoffs", () => jobs(ws)), button("Copy workspace ID", async () => { try { await navigator.clipboard.writeText(ws.id); message("Workspace ID copied."); } catch { show("Workspace ID", ws.id); } }));
    const details = node("details"); details.append(node("summary", "Exclusions & policy"));
    details.append(node("p", "Built-in secret, dependency and build exclusions cannot be removed. Extra patterns are administrator-owned; repository ignore files do not control access.", "muted"));
    const area = node("textarea"); area.rows = 3; area.value = JSON.parse(ws.excludes).join("\n"); details.append(area, button("Save exclusions", () => { if (confirm("Change which files future browsing can access?")) return manage(ws.id, "set_excludes", area.value.split("\n").map(x=>x.trim()).filter(Boolean)); }));
    const policyLabel = node("label", "Write permission");
    const select = node("select"); select.setAttribute("aria-label", `Write permission for ${ws.name}`);
    for (const [value, label] of [["none", "Read-only — no file writes"], ["handoff", "Handoff only (default)"], ["workspace", "Workspace-wide — allowed text files"]]) {
      const option = node("option", label); option.value = value; select.append(option);
    }
    select.value = ws.write_scope; policyLabel.append(select);
    details.append(policyLabel, button("Save write permission", async () => {
      const scope = select.value;
      if (scope === ws.write_scope) return;
      const warning = scope === "workspace"
        ? "Allow every chat using this shared connection to create/replace/edit permitted SOURCE files in this workspace? Exclusions remain enforced. This does not start an agent."
        : scope === "none" ? "Disable all file writes in this workspace, including new handoffs? Reads will still work."
        : "Restrict all future writes to .workspace-handoff/? Source files will become read-only.";
      if (!confirm(warning)) { select.value = ws.write_scope; return; }
      await api(`/api/workspaces/${ws.id}`, "POST", {operation: "set_write_scope", write_scope: scope});
      await refresh(); message("Write policy saved. It applies to subsequent calls; no tunnel restart needed.");
    }));
    row.append(actions, details); $("workspaces").append(row);
  }
  const names = Object.fromEntries(data.workspaces.map(w => [w.id, w.name])); $("events").replaceChildren();
  for (const e of history.events) { const tr = node("tr"); for (const value of [new Date(e.at).toLocaleString(), names[e.workspace] || "Admin", e.action, e.outcome]) tr.append(node("td", value)); $("events").append(tr); }
}
$("login-form").onsubmit = async (event) => { event.preventDefault(); adminToken = $("admin-token").value.trim(); $("admin-token").value = ""; try { config = await api("/api/status"); $("status-line").textContent = `Version ${config.version} · ${config.listen_mode === "docker-published-loopback" ? "Docker · host loopback only" : "Loopback-only"} · General file tools with per-workspace write permissions`; $("parents").textContent = `Approved project parents: ${config.allowed_parents.join(", ")}`; await refresh(); $("login").hidden = true; $("dashboard").hidden = false; } catch(e) { adminToken = ""; message(e.message); } };
$("logout").onclick = () => { adminToken = ""; currentWorkspace = null; $("dashboard").hidden = true; $("login").hidden = false; $("output-panel").hidden = true; $("output").textContent = ""; $("workspaces").replaceChildren(); $("jobs").replaceChildren(); $("events").replaceChildren(); $("jobs-panel").hidden = true; };
$("close-output").onclick = () => { $("output-panel").hidden = true; $("output").textContent = ""; };
$("refresh").onclick = () => refresh().catch(e=>message(e.message));
$("add-form").onsubmit = async (event) => { event.preventDefault(); const form = event.target; const data = new FormData(form); try { const result = await api("/api/workspaces", "POST", {name:data.get("name"), root:data.get("root"), excludes:String(data.get("excludes")).split("\n").map(x=>x.trim()).filter(Boolean)}); form.reset(); message(result.note); await refresh(); } catch(e) { message(e.message); } };

$("bridge-profile").onclick = () => tunnelProfile();
$("bridge-rotate").onclick = async () => {
  if (!confirm("Create a new shared bridge credential? It authorizes ALL enabled mappings, enables MCP access, and revokes the previous bridge token.")) return;
  try { const result = await api("/api/bridge", "POST", {operation:"rotate_token"}); credential(result); await refresh(); }
  catch(e) { message(e.message); }
};
$("bridge-pause").onclick = async () => {
  try { await api("/api/bridge", "POST", {operation:bridgeState.enabled ? "disable" : "enable"}); await refresh(); }
  catch(e) { message(e.message); }
};
