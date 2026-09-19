"use strict";
// All untrusted names, paths, reports, and server responses use textContent.
// Authentication uses an HttpOnly session cookie (same-origin only). The admin token is
// exchanged once via POST /api/login and never stored in localStorage/sessionStorage/document.cookie.
// Only a non-sensitive theme preference is kept in localStorage. No third-party assets.
// Agent-controlled text (run results, transcripts, permission metadata) is never
// rendered as HTML.
let currentWorkspace = null;
let config = {};
let bridgeState = {};
let settings = {model_policy: {configured: false, enabled: [], default: null, enabled_count: 0}};
let globalModels = [];
let sessionsOffset = 0;
const SESSIONS_PAGE = 25;
const $ = (id) => document.getElementById(id);
function theme() { return document.documentElement.getAttribute("data-theme") || (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"); }
function renderTheme() { $("theme-toggle").textContent = theme() === "dark" ? "◐ Light" : "◐ Dark"; $("theme-toggle").setAttribute("aria-label", `Switch to ${theme() === "dark" ? "light" : "dark"} mode`); }
$("theme-toggle").onclick = () => {
  const next = theme() === "dark" ? "light" : "dark";
  if (next === (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")) document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", next);
  try { const current = document.documentElement.getAttribute("data-theme"); if (current) localStorage.setItem("wb-theme", current); else localStorage.removeItem("wb-theme"); } catch {}
  renderTheme();
};
renderTheme();
function node(tag, text, cls) { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n; }
function message(text) { $("message").textContent = text; $("message").style.display = "block"; setTimeout(() => { $("message").style.display = "none"; }, 6000); }
async function api(path, method = "GET", body) {
  const r = await fetch(path, {method, credentials: "same-origin", headers: {...(body ? {"Content-Type": "application/json"} : {})}, body: body ? JSON.stringify(body) : undefined});
  const value = await r.json(); if (!r.ok) throw new Error(value.error || `HTTP ${r.status}`); return value;
}
function button(text, fn, cls = "secondary") { const b = node("button", text, cls); b.type = "button"; b.onclick = () => Promise.resolve().then(fn).catch(e => message(e.message)); return b; }
function show(title, value) { $("output-title").textContent = title; $("output").textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2); $("output-panel").hidden = false; $("output-panel").scrollIntoView({behavior: "smooth", block: "center"}); }
function credential(value) { show("Shared bridge credential — shown once", `This credential authorizes ALL enabled workspace mappings. Save it in your local tunnel environment, NOT in ChatGPT.\n\n${value.token}\n\nAfter rotation, update the tunnel environment and restart tunnel-client. The old token is revoked for subsequent calls.`); }
async function manage(id, operation, extra) {
  const result = await api(`/api/workspaces/${id}`, "POST", {operation, ...(extra || {})});
  if (result.token) credential(result); await refresh();
}
function tunnelProfile() {
  const profile = `config_version: 1\ncontrol_plane:\n  tunnel_id: tunnel_REPLACE_WITH_YOUR_32_HEX_ID\n  api_key: env:CONTROL_PLANE_API_KEY\nmcp:\n  server_urls:\n    - channel: main\n      url: http://127.0.0.1:${config.mcp_port}/mcp\n  extra_headers:\n    X-Bridge-Token: env:WORKSPACE_BRIDGE_TOKEN\n  discovery_extra_headers:\n    X-Bridge-Token: env:WORKSPACE_BRIDGE_TOKEN\nhealth:\n  listen_addr: 127.0.0.1:8790\n`;
  show("Tunnel profile (contains no credentials)", profile + "\nSave outside your project as bridge-tunnel.yaml. Supply keys locally, then run:\n\ntunnel-client doctor --config /absolute/path/bridge-tunnel.yaml --explain\ntunnel-client run --config /absolute/path/bridge-tunnel.yaml\n\nUse this ONE tunnel and ONE ChatGPT connection for all enabled mappings. Adding workspaces requires no new tunnel. Never tunnel the management listener.");
}

function renderRuntime(status) {
  const runtime = status.opencode || {};
  const pill = $("opencode-status");
  const label = !runtime.configured ? "Not configured" : runtime.locked ? "Locked" : runtime.healthy ? "Healthy" : "Unavailable";
  pill.textContent = label;
  pill.className = runtime.configured && runtime.healthy ? "pill enabled" : "pill";
  $("opencode-endpoint").textContent = runtime.configured ? "Bridge → private SDK adapter → host OpenCode server" : "Set WB_OPENCODE_RUNTIME_URL to connect the private adapter.";
  const parts = [];
  if (runtime.version) parts.push(`OpenCode ${runtime.version}`);
  if (runtime.adapter_version) parts.push(`adapter ${runtime.adapter_version}`);
  if (runtime.locked) parts.push("Adapter locked: set WB_RUNTIME_TOKEN (bridge and adapter) to unlock agent operations.");
  if (runtime.configured && !runtime.healthy && runtime.detail) parts.push(runtime.detail);
  if (!runtime.configured) parts.push("Agent execution stays disabled until the local admin enables it per workspace.");
  $("opencode-meta").textContent = parts.join(" · ");
  $("discord-status").textContent = runtime.discord_configured
    ? "Discord notifications: configured. Waiting and completion notices are sent with safe metadata only."
    : "Discord notifications: not configured (optional). No webhook secret is exposed here.";
  const policy = settings.model_policy || {configured: false, enabled: [], default: null, enabled_count: 0};
  $("policy-status").textContent = policy.configured
    ? `Policy: ${policy.enabled_count} enabled model(s) · default ${policy.default}. Omit the model to use the default; explicit models must be enabled and available.`
    : "No model policy saved yet: new OpenCode runs are rejected until you enable at least one model and a default.";
}

// Model management lives in a lazily loaded <dialog>: the main page shows
// only the compact summary above. Draft checkbox/select state is kept in
// module vars so Cancel/close discards it; only Save posts to the server.
let draftEnabled = new Set();
let draftDefault = null;
function renderModelPolicy() {
  const container = $("model-policy");
  container.replaceChildren();
  const filter = ($("model-filter") && $("model-filter").value || "").trim().toLowerCase();
  const select = $("policy-default");
  select.replaceChildren();
  const visible = globalModels.filter((m) => !filter || (m.selector || "").toLowerCase().includes(filter) || (m.name || "").toLowerCase().includes(filter));
  if (!globalModels.length) {
    container.append(node("p", "No models discovered yet. Press “Refresh global models”.", "muted"));
  } else if (!visible.length) {
    container.append(node("p", "No models match the current filter.", "muted"));
  }
  for (const m of visible) {
    const row = node("div");
    const label = node("label", `${m.name || m.model} · ${m.selector}${draftEnabled.has(m.selector) ? "" : " (disabled)"}`);
    const enable = node("input"); enable.type = "checkbox"; enable.checked = draftEnabled.has(m.selector);
    enable.setAttribute("aria-label", `Enable ${m.selector}`);
    enable.onchange = () => {
      if (enable.checked) draftEnabled.add(m.selector);
      else {
        draftEnabled.delete(m.selector);
        if (draftDefault === m.selector) draftDefault = null;
      }
      renderModelPolicy();
    };
    label.prepend(enable); row.append(label); container.append(row);
  }
  // The default dropdown is populated only from currently enabled
  // selections and updates immediately when they change. If the current
  // default was disabled, no silent replacement is picked: the admin must
  // choose explicitly before saving.
  const enabledNow = globalModels.filter((m) => draftEnabled.has(m.selector));
  const placeholder = node("option", "Select a default among enabled models"); placeholder.value = "";
  select.append(placeholder);
  for (const m of enabledNow) {
    const option = node("option", `${m.name || m.model} · ${m.selector}`); option.value = m.selector;
    select.append(option);
  }
  select.value = draftDefault && draftEnabled.has(draftDefault) ? draftDefault : "";
  if (!select.value) draftDefault = null;
}

async function openModels() {
  try {
    await loadModels();
  } catch (e) { message(e.message); }
  const policy = settings.model_policy || {enabled: [], default: null};
  draftEnabled = new Set(policy.enabled || []);
  draftDefault = policy.default || null;
  if ($("model-filter")) $("model-filter").value = "";
  renderModelPolicy();
  const dialog = $("models-dialog");
  if (dialog && typeof dialog.showModal === "function") dialog.showModal();
  else message("This browser does not support the model management dialog.");
}

async function loadModels() {
  const data = await api("/api/opencode/models?limit=100");
  globalModels = data.models || [];
  if (data.policy) settings.model_policy = data.policy;
  renderModelPolicy();
  renderRuntime(config);
  if (!globalModels.length) message("No models are available in the current global OpenCode runtime.");
}

async function saveModelPolicy() {
  const enabled = globalModels.filter((m) => draftEnabled.has(m.selector)).map((m) => m.selector);
  const def = $("policy-default").value || null;
  if (!enabled.length) { message("Enable at least one model before saving."); return; }
  if (!def || !draftEnabled.has(def)) { message("Choose the mandatory default among the enabled models."); return; }
  if (!confirm(`Save global model policy with ${enabled.length} enabled model(s) and mandatory default ${def}? Runs without a model use the default; explicit models must be enabled.`)) return;
  const saved = await api("/api/settings", "POST", {enabled, default: def});
  settings = {model_policy: saved};
  draftEnabled = new Set(saved.enabled || []);
  draftDefault = saved.default || null;
  const dialog = $("models-dialog");
  if (dialog && typeof dialog.close === "function") dialog.close();
  await loadModels();
  message(`Model policy saved. Default: ${def}.`);
}

function runRow(ws, run) {
  const row = node("div", undefined, "job");
  const title = node("h3", `${run.state} · ${run.model || "OpenCode default model"}`);
  row.append(title, node("div", `Run ${run.run_id} · Session ${run.session_id || "—"} · Job ${run.job_id} · Request ${run.request_id}`, "path"));
  if (run.session_reused) row.append(node("div", `Reused OpenCode session · Continued from ${run.continue_from_run_id || run.parent_run_id || "—"}`, "muted"));
  const times = [`created ${run.created ? new Date(run.created).toLocaleString() : "—"}`];
  if (run.started) times.push(`started ${new Date(run.started).toLocaleString()}`);
  if (run.finished) times.push(`finished ${new Date(run.finished).toLocaleString()}`);
  if (run.duration_seconds !== null && run.duration_seconds !== undefined) times.push(`${run.duration_seconds}s`);
  times.push(`notification ${(run.notification && run.notification.status) || "none"}`);
  row.append(node("div", times.join(" · "), "muted"));
  const actions = node("div", undefined, "actions");
  actions.append(button("View session", async () => { const data = await api(`/api/runs/${run.run_id}/session`); const lines = (data.transcript || []).map(m => `[${m.role}] ${m.error ? "ERROR " + m.error + "\n" : ""}${m.text || ""}${m.tools && m.tools.length ? "\ntools: " + m.tools.join(", ") : ""}`).join("\n\n"); show(`Session ${run.session_id || ""} (escaped, bounded)`, lines || "No messages yet."); }));
  actions.append(button("Run details", async () => { const data = await api(`/api/runs/${run.run_id}`); show(`Run ${run.run_id}`, data); }));
  if (run.active) actions.append(button("Stop", () => stopRun(run.run_id), "danger"));
  if (run.pending_request_count > 0) actions.append(button(`Requests (${run.pending_request_count})`, () => requestView(ws, run.run_id)));
  row.append(actions);
  return row;
}

async function stopRun(runId) {
  if (!confirm("Interrupt only this recorded OpenCode session? No process is killed directly.")) return;
  const result = await api(`/api/runs/${runId}/stop`, "POST");
  message(result.cancelled ? "Session interrupted; run marked cancelled." : `Run state unchanged (${result.state}).`);
  await refresh(); if (currentWorkspace) await jobs(currentWorkspace);
}

function sessionRow(run) {
  const row = node("tr");
  row.append(node("td", run.state || "—"));
  row.append(node("td", run.workspace_name || run.workspace_id || "—"));
  const handoffCell = node("td");
  handoffCell.append(node("div", run.handoff_title || run.job_id || "—"));
  if (run.session_reused) handoffCell.append(node("div", `Continued from ${run.continue_from_run_id || run.parent_run_id || "—"}`, "muted"));
  row.append(handoffCell);
  row.append(node("td", run.model || "—"));
  const sessionCell = node("td");
  sessionCell.append(node("div", run.session_id || "—"));
  if (run.session_reused) sessionCell.append(node("div", "reused", "muted"));
  row.append(sessionCell);
  const times = [`created ${run.created ? new Date(run.created).toLocaleString() : "—"}`];
  if (run.started) times.push(`started ${new Date(run.started).toLocaleString()}`);
  if (run.finished) times.push(`finished ${new Date(run.finished).toLocaleString()}`);
  else if (run.updated) times.push(`updated ${new Date(run.updated).toLocaleString()}`);
  if (run.duration_seconds !== null && run.duration_seconds !== undefined) times.push(`${run.duration_seconds}s`);
  row.append(node("td", times.join(" · "), "muted"));
  const pending = run.pending_request_count || 0;
  const pendingCell = node("td", pending > 0 ? `${pending} pending · needs attention` : "0");
  if (pending > 0) pendingCell.className = "attention";
  row.append(pendingCell);
  row.append(node("td", (run.notification && run.notification.status) || "none"));
  const actionsCell = node("td");
  const actions = node("div", undefined, "actions");
  actions.append(button("View details", async () => { const data = await api(`/api/runs/${run.run_id}/session`); show(`Session ${run.session_id || ""} (escaped, bounded)`, data); }));
  if (run.active) actions.append(button("Stop", () => stopRun(run.run_id), "danger"));
  if (pending > 0) actions.append(button(`Requests (${pending})`, async () => { const data = await api(`/api/runs/${run.run_id}`); show(`Run ${run.run_id}`, data); }));
  actionsCell.append(actions);
  row.append(actionsCell);
  return row;
}

async function loadSessions(reset) {
  if (reset) { sessionsOffset = 0; $("sessions").replaceChildren(); }
  const data = await api(`/api/opencode/sessions?${new URLSearchParams({offset: String(sessionsOffset), limit: String(SESSIONS_PAGE)})}`);
  $("sessions-count").textContent = `Bridge-owned · all workspaces · ${data.runs.length} shown`;
  if (reset && !data.runs.length) { const empty = node("tr"); const cell = node("td", "No Bridge-owned OpenCode sessions yet.", "muted"); cell.colSpan = 9; empty.append(cell); $("sessions").append(empty); }
  for (const run of data.runs) $("sessions").append(sessionRow(run));
  sessionsOffset += data.runs.length;
  $("more-sessions").disabled = data.next_offset === null || data.next_offset === undefined;
}

async function requestView(ws, runId) {
  const run = await api(`/api/runs/${runId}`);
  const container = node("div");
  for (const req of run.pending_requests || []) {
    container.append(node("h3", `${req.kind} · ${req.action || ""}`), node("div", `Request ${req.request_id}`, "path"));
    if (req.title) container.append(node("p", req.title));
    container.append(node("div", `Proposed always scope: ${JSON.stringify(req.pattern)}${req.redacted ? " (metadata redacted)" : ""}`, "muted"));
    if (req.metadata && Object.keys(req.metadata).length) container.append(node("div", JSON.stringify(req.metadata), "path"));
    const actions = node("div", undefined, "actions");
    actions.append(button("Approve once", () => respond(runId, req.request_id, "once")));
    if (req.always_allowed) actions.append(button("Approve always (exact scope)", () => respond(runId, req.request_id, "always"), "danger"));
    else actions.append(node("span", "always unavailable: no reviewable scope", "muted"));
    actions.append(button("Reject", () => respond(runId, req.request_id, "reject"), "danger"));
    container.append(actions);
  }
  if (!(run.pending_requests || []).length) container.append(node("p", "No pending requests.", "muted"));
  $("output-title").textContent = `Pending requests for ${runId}`;
  $("output").textContent = ""; $("output").append(container); $("output-panel").hidden = false; $("output-panel").scrollIntoView({behavior: "smooth", block: "center"});
}

async function respond(runId, requestId, decision) {
  const warning = decision === "always"
    ? "Approve OpenCode's EXACT proposed pattern for this session? Bridge never broadens it. Review the scope above first."
    : decision === "once" ? "Approve only this request?" : "Reject this request?";
  if (!confirm(warning)) return;
  const result = await api(`/api/runs/${runId}/requests/${requestId}`, "POST", {decision});
  message(`Permission ${result.decision} recorded. Run state: ${result.run_state}. Same session resumes.`);
  await refresh(); if (currentWorkspace) await jobs(currentWorkspace);
}

async function jobs(ws) {
  currentWorkspace = ws;
  const [data, runs] = await Promise.all([api(`/api/workspaces/${ws.id}/jobs`), api(`/api/workspaces/${ws.id}/runs`)]);
  $("jobs-title").textContent = `${ws.name} · Handoffs & runs`; $("jobs-panel").hidden = false; $("jobs").replaceChildren();
  $("jobs").append(node("h3", "Handoffs"));
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
  $("jobs").append(node("h3", "Runs"));
  if (!runs.runs.length) $("jobs").append(node("p", "No OpenCode runs yet. Enable agent execution for this workspace, then ask ChatGPT to start the prepared handoff.", "muted"));
  for (const run of runs.runs) $("jobs").append(runRow(ws, run));
  if (data.next_offset !== null) $("jobs").append(node("p", "Showing the most recent 40 handoffs. Use the paginated MCP tool for older entries.", "muted"));
}

async function refresh() {
  const [data, history, status, currentSettings] = await Promise.all([api("/api/workspaces"), api("/api/events"), api("/api/status"), api("/api/settings")]);
  config = status; bridgeState = status.bridge; settings = currentSettings;
  renderRuntime(status);
  $("bridge-status").textContent = !bridgeState.configured ? "Not configured" : bridgeState.enabled ? "Enabled" : "Paused";
  $("bridge-pause").textContent = bridgeState.enabled ? "Pause all MCP access" : "Enable MCP access";
  $("bridge-pause").disabled = !bridgeState.configured;
  $("bridge-rotate").textContent = bridgeState.configured ? "Rotate bridge token" : "Create bridge token";
  $("shared-endpoint").textContent = `http://127.0.0.1:${config.mcp_port}/mcp`;
  $("workspace-count").textContent = data.workspaces.length; $("workspaces").replaceChildren();
  if (!data.workspaces.length) $("workspaces").append(node("p", "No projects are exposed. Add a workspace below.", "muted"));
  for (const ws of data.workspaces) {
    const row = node("div", undefined, "workspace"); const title = node("h3", ws.name); title.append(node("span", ws.enabled ? "Enabled" : "Disabled", ws.enabled ? "enabled" : "disabled"));
    row.append(title, node("div", ws.root, "path"), node("div", `Workspace ID: ${ws.id} · Write scope: ${ws.write_scope} · Agent execution: ${ws.agent_enabled ? "enabled" : "disabled"}`, "path"));
    const actions = node("div", undefined, "actions");
    // Policy controls first: workspace access immediately followed by the agent toggle.
    actions.append(button(ws.enabled ? "Disable access" : "Enable access", () => { if (ws.enabled || confirm("Expose this mapping to every chat using the shared bridge connection?")) return manage(ws.id, ws.enabled ? "disable" : "enable"); }, ws.enabled ? "danger" : "secondary"));
    actions.append(button(ws.agent_enabled ? "Disable agent" : "Enable agent", async () => {
      const enabled = !ws.agent_enabled;
      const warning = enabled
        ? "Enable OpenCode agent execution for this workspace? Any holder of the shared bridge credential can then start bounded runs for prepared handoffs here. Runs can read/write files under the workspace with the host OpenCode server's authority and may request permissions. This is independent of write permission and default OFF."
        : "Disable OpenCode agent execution for this workspace? Existing runs are not stopped automatically.";
      if (!confirm(warning)) return;
      await api(`/api/workspaces/${ws.id}`, "POST", {operation: "set_agent_enabled", agent_enabled: enabled});
      await refresh(); message(`Agent execution ${enabled ? "enabled" : "disabled"}. It is independent of write scope.`);
    }, ws.agent_enabled ? "danger" : "secondary"));
    actions.append(button("Handoffs & runs", () => jobs(ws)), button("Copy workspace ID", async () => { try { await navigator.clipboard.writeText(ws.id); message("Workspace ID copied."); } catch { show("Workspace ID", ws.id); } }));
    const details = node("details"); details.append(node("summary", "Exclusions & policy"));
    details.append(node("p", "Built-in secret, dependency and build exclusions cannot be removed. Extra patterns are administrator-owned; repository ignore files do not control access.", "muted"));
    const area = node("textarea"); area.rows = 3; area.value = JSON.parse(ws.excludes).join("\n"); details.append(area, button("Save exclusions", () => { if (confirm("Change which files future browsing can access?")) return manage(ws.id, "set_excludes", {excludes: area.value.split("\n").map(x=>x.trim()).filter(Boolean)}); }));
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
        : "Restrict all writes to .workspace-handoff/? Source files will become read-only.";
      if (!confirm(warning)) { select.value = ws.write_scope; return; }
      await api(`/api/workspaces/${ws.id}`, "POST", {operation: "set_write_scope", write_scope: scope});
      await refresh(); message("Write policy saved. It applies to subsequent calls; no tunnel restart needed.");
    }));
    row.append(actions, details); $("workspaces").append(row);
  }
  const names = Object.fromEntries(data.workspaces.map(w => [w.id, w.name])); $("events").replaceChildren();
  for (const e of history.events) { const tr = node("tr"); for (const value of [new Date(e.at).toLocaleString(), names[e.workspace] || "Admin", e.action, e.outcome]) tr.append(node("td", value)); $("events").append(tr); }
  // Global sessions refresh independently; model discovery is lazy (dialog
  // open / explicit refresh) so it never blocks workspace/policy rendering.
  await Promise.allSettled([loadSessions(true)]);
}
$("login-form").onsubmit = async (event) => {
  event.preventDefault();
  const token = $("admin-token").value.trim();
  $("admin-token").value = "";
  try {
    const r = await fetch("/api/login", {method: "POST", credentials: "same-origin", headers: {"Authorization": `Bearer ${token}`}});
    if (!r.ok) { const value = await r.json(); throw new Error(value.error || `HTTP ${r.status}`); }
    config = await api("/api/status");
    $("status-line").textContent = `Version ${config.version} · ${config.listen_mode === "docker-published-loopback" ? "Docker · host loopback only" : "Loopback-only"} · General file tools with per-workspace write and agent policies`;
    $("parents").textContent = `Approved project parents: ${config.allowed_parents.join(", ")}`;
    await refresh();
    $("boot").hidden = true;
    $("login").hidden = true;
    $("dashboard").hidden = false;
  } catch(e) { message(e.message); }
};
async function tryRestoreSession() {
  // Initial HTML shows only #boot: #login and #dashboard both start hidden,
  // so a valid session transitions directly to the dashboard with no login flash.
  try {
    const r = await fetch("/api/status", {credentials: "same-origin"});
    if (r.status !== 200) { $("boot").hidden = true; $("login").hidden = false; return; }
    config = await r.json();
    $("status-line").textContent = `Version ${config.version} · ${config.listen_mode === "docker-published-loopback" ? "Docker · host loopback only" : "Loopback-only"} · General file tools with per-workspace write and agent policies`;
    $("parents").textContent = `Approved project parents: ${config.allowed_parents.join(", ")}`;
    await refresh();
    $("boot").hidden = true;
    $("dashboard").hidden = false;
  } catch {
    $("boot").hidden = true; $("login").hidden = false;
  }
}
tryRestoreSession();
$("logout").onclick = async () => {
  try { await fetch("/api/logout", {method: "POST", credentials: "same-origin"}); } catch {}
  currentWorkspace = null; globalModels = []; sessionsOffset = 0;
  $("dashboard").hidden = true; $("boot").hidden = true; $("login").hidden = false; $("output-panel").hidden = true; $("output").replaceChildren(); $("output").textContent = ""; $("workspaces").replaceChildren(); $("jobs").replaceChildren(); $("events").replaceChildren(); $("sessions").replaceChildren(); $("jobs-panel").hidden = true;
};
$("close-output").onclick = () => { $("output-panel").hidden = true; $("output").textContent = ""; };
$("refresh").onclick = () => refresh().catch(e=>message(e.message));
$("open-models").onclick = () => openModels().catch(e=>message(e.message));
$("load-models").onclick = () => loadModels().catch(e=>message(e.message));
$("save-policy").onclick = () => saveModelPolicy().catch(e=>message(e.message));
$("cancel-policy").onclick = () => { const dialog = $("models-dialog"); if (dialog && typeof dialog.close === "function") dialog.close(); };
$("model-filter").oninput = () => renderModelPolicy();
$("policy-default").onchange = (event) => { draftDefault = event.target.value || null; };
$("refresh-sessions").onclick = () => loadSessions(true).catch(e=>message(e.message));
$("more-sessions").onclick = () => loadSessions(false).catch(e=>message(e.message));
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
