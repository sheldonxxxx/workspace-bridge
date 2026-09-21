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
let settings = {model_policy: {configured: false, enabled: [], default: null, enabled_count: 0}, runtime_policies: {}};
let globalModels = [];
let modelRuntime = "opencode";
let discoveryWorkspaceId = null;
let allWorkspaces = [];
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
  $("opencode-endpoint").textContent = runtime.configured ? "Adapter connected · host OpenCode" : "Adapter not configured";
  const parts = [];
  if (runtime.version) parts.push(`OpenCode ${runtime.version}`);
  if (runtime.adapter_version) parts.push(`adapter ${runtime.adapter_version}`);
  if (runtime.locked) parts.push("Adapter locked");
  if (runtime.configured && !runtime.healthy && runtime.detail) parts.push(runtime.detail);
  if (!runtime.configured) parts.push("Agent runs are off by default");
  $("opencode-meta").textContent = parts.join(" · ");
  $("discord-status").textContent = runtime.discord_configured
    ? "Discord notifications on"
    : "Discord notifications off";
  const policy = settings.model_policy || {configured: false, enabled: [], default: null, enabled_count: 0};
  $("policy-status").textContent = policy.configured
    ? `Policy: ${policy.enabled_count} enabled · default ${policy.default}`
    : "Policy: not configured · choose an enabled model and default";
  const runtimes = (status.runtimes && status.runtimes.runtimes) || {};
  const pi = runtimes.pi || {};
  const piPolicies = status.runtime_policies || settings.runtime_policies || {};
  const piPolicy = piPolicies.pi || {configured: false, enabled: [], default: null, enabled_count: 0};
  if ($("pi-status")) {
    const piPill = $("pi-status");
    const piLabel = !pi.configured ? "Not configured" : pi.locked ? "Locked" : pi.healthy ? "Healthy" : "Unavailable";
    piPill.textContent = piLabel;
    piPill.className = pi.configured && pi.healthy ? "pill enabled" : "pill";
  }
  if ($("pi-endpoint")) $("pi-endpoint").textContent = pi.configured ? "Adapter connected · host Pi" : "Pi adapter not configured";
  if ($("pi-meta")) {
    const piParts = [];
    if (pi.version) piParts.push(`Pi ${pi.version}`);
    if (pi.adapter_version) piParts.push(`adapter ${pi.adapter_version}`);
    if (pi.locked) piParts.push("Adapter locked");
    if (pi.configured && !pi.healthy && pi.detail) piParts.push(pi.detail);
    if (!pi.configured) piParts.push("Pi runs are off by default");
    $("pi-meta").textContent = piParts.join(" · ");
  }
  if ($("pi-policy-status")) $("pi-policy-status").textContent = piPolicy.configured
    ? `Policy: ${piPolicy.enabled_count} enabled · default ${piPolicy.default}`
    : "Policy: not configured · choose an enabled model and default";
  const piPerms = (status.runtime_permissions && status.runtime_permissions.pi)
    || (settings.runtime_permissions && settings.runtime_permissions.pi)
    || null;
  if ($("pi-permission-status")) {
    let permText;
    if (!piPerms) {
      permText = "Permissions: loading…";
    } else if (piPerms.enabled) {
      permText =
        `Permissions: Approval mode enabled · rev ${String(piPerms.policy_revision || "").slice(0, 12)} · new sessions only`;
    } else {
      permText = "Permissions: Read-only · new sessions only";
    }
    // Bounded external scope summary only (never host root paths).
    if (piPerms && piPerms.external_default_mode) {
      const rootCount = Number(piPerms.external_root_count) || 0;
      permText += ` · outside ${piPerms.external_default_mode} (${rootCount} root${rootCount === 1 ? "" : "s"})`;
    }
    // Deployed adapter readiness comes from bounded runtime diagnostics
    // (never paths or policy bodies): writable sessions need an adapter
    // that advertises both permission capabilities.
    const piHealth = (pi && pi.health) || {};
    if ("permissions_supported" in piHealth) {
      permText += piHealth.permissions_supported ? " · adapter ready" : " · adapter update required";
    }
    $("pi-permission-status").textContent = permText;
  }
}

// Pi file permission policy lives in a dedicated <dialog>: structured
// controls only, never raw JSON. Draft state is kept in module vars so
// Cancel/close discards it; only Save posts to the server.
const PI_PERM_TOOLS = ["read", "grep", "find", "ls", "edit", "write"];
const PI_PERM_MODES = ["allow", "ask", "deny"];
let piPermCurrent = null;
let piPermDraft = null;
function piPermDraftFrom(policy) {
  const external = policy.external_access || {};
  return {
    enabled: Boolean(policy.write_tools_enabled ?? policy.enabled),
    tools: {...policy.tools},
    protected_patterns: [...(policy.protected_patterns || [])],
    protected_template_exceptions: [...(policy.protected_template_exceptions || [])],
    allow_session_always: Boolean(policy.allow_session_always),
    external_default_mode: ["allow", "ask", "deny"].includes(external.default_mode)
      ? external.default_mode : "deny",
    external_roots: (external.roots || []).map((r) => ({
      path: String(r.path || ""),
      mode: ["allow", "ask", "deny"].includes(r.mode) ? r.mode : "deny",
    })),
    // v3 shell authority: single Deny/Ask/Allow selector, no command rules.
    // v2 policies migrate in memory with shell deny.
    shell_mode: ["deny", "ask", "allow"].includes(policy.shell_mode) ? policy.shell_mode : "deny",
  };
}
function renderPiExternalRoots() {
  const draft = piPermDraft;
  const box = $("pi-perm-roots");
  if (!draft || !box) return;
  box.replaceChildren();
  if (!draft.external_roots.length) {
    box.append(node("p", "No external roots. Every outside-workspace path uses the default mode above.", "muted"));
  }
  draft.external_roots.forEach((root, index) => {
    const row = node("div", undefined, "pi-perm-root");
    const pathInput = node("input");
    pathInput.type = "text";
    pathInput.value = root.path;
    pathInput.placeholder = "/absolute/native/macos/path";
    pathInput.setAttribute("aria-label", `External root ${index + 1} path`);
    pathInput.spellcheck = false;
    pathInput.onchange = () => { root.path = pathInput.value.trim(); renderPiExternalWarning(); };
    const modeSelect = node("select");
    modeSelect.setAttribute("aria-label", `External root ${index + 1} mode`);
    for (const mode of PI_PERM_MODES) {
      const option = node("option", mode[0].toUpperCase() + mode.slice(1));
      option.value = mode;
      modeSelect.append(option);
    }
    modeSelect.value = root.mode;
    modeSelect.onchange = () => { root.mode = modeSelect.value; renderPiExternalWarning(); };
    const remove = node("button", "Remove", "secondary");
    remove.type = "button";
    remove.onclick = () => {
      draft.external_roots.splice(index, 1);
      renderPiExternalRoots();
    };
    row.append(pathInput, modeSelect, remove);
    box.append(row);
  });
  const defaultSelect = $("pi-perm-external-default");
  if (defaultSelect) defaultSelect.value = draft.external_default_mode;
  renderPiExternalWarning();
}
function renderPiExternalWarning() {
  const draft = piPermDraft;
  const warn = $("pi-perm-external-warning");
  if (!draft || !warn) return;
  const current = piPermCurrent ? piPermCurrent.policy : null;
  const currentDefault = (current && current.external_access && current.external_access.default_mode) || "deny";
  const warnings = [];
  if (draft.external_default_mode !== "deny") {
    warnings.push(`Default outside-workspace mode is ${draft.external_default_mode.toUpperCase()}: every unlisted host path is ${draft.external_default_mode === "allow" ? "readable/writable without asking" : "ask-gated"}.`);
  } else if (currentDefault !== "deny" && draft.external_default_mode === "deny") {
    warnings.push("Default outside-workspace mode returns to Deny.");
  }
  for (const root of draft.external_roots) {
    if (root.path === "/") {
      warnings.push("Root “/” exposes the entire host filesystem — configure only if you fully trust every Pi session.");
      break;
    }
  }
  const broadened = draft.external_roots.filter((r) => r.mode !== "deny");
  if (broadened.length) {
    warnings.push(`${broadened.length} root${broadened.length === 1 ? "" : "s"} broaden${broadened.length === 1 ? "s" : ""} access beyond deny; most-specific match wins.`);
  }
  warn.hidden = !warnings.length;
  warn.textContent = warnings.join(" ");
}
function renderPiShellWarning() {
  const draft = piPermDraft;
  const warn = $("pi-perm-shell-warning");
  if (!draft || !warn) return;
  if (draft.shell_mode === "allow") {
    warn.hidden = false;
    warn.textContent = "Shell Allow runs with native macOS-user authority and can bypass structured file path controls.";
  } else if (draft.shell_mode === "ask") {
    warn.hidden = false;
    warn.textContent = "Shell Ask pauses each bash invocation for approval (exact command + timeout; once approves the exact call, always is exact-command scoped).";
  } else {
    warn.hidden = true;
    warn.textContent = "";
  }
}
function renderPiPermissions() {
  const draft = piPermDraft;
  if (!draft) return;
  $("pi-perm-enabled").checked = draft.enabled;
  const toolsBox = $("pi-perm-tools");
  toolsBox.replaceChildren();
  const tableTitle = node("p", "Per-tool mode (Allow / Ask / Deny). Read, search, and list policy is enforced in every session; when the writable switch above is off, edit/write stay unexposed and their draft below is preserved but inactive.", "muted");
  toolsBox.append(tableTitle);
  for (const tool of PI_PERM_TOOLS) {
    const label = node("label", `${tool}`);
    const select = node("select");
    select.setAttribute("aria-label", `Pi ${tool} mode`);
    select.setAttribute("data-pi-perm-tool", tool);
    for (const mode of ["allow", "ask", "deny"]) {
      const option = node("option", mode[0].toUpperCase() + mode.slice(1));
      option.value = mode;
      select.append(option);
    }
    select.value = draft.tools[tool] || "deny";
    if (!draft.enabled && (tool === "edit" || tool === "write")) {
      label.append(node("span", " (inactive while disabled)", "muted"));
    }
    select.onchange = () => { draft.tools[tool] = select.value; };
    label.append(select);
    toolsBox.append(label);
  }
  $("pi-perm-always").checked = draft.allow_session_always;
  $("pi-perm-protected").value = draft.protected_patterns.join("\n");
  $("pi-perm-exceptions").value = draft.protected_template_exceptions.join("\n");
  const shellSelect = $("pi-perm-shell");
  if (shellSelect) {
    shellSelect.value = draft.shell_mode || "deny";
    shellSelect.onchange = () => { draft.shell_mode = shellSelect.value; renderPiShellWarning(); };
  }
  renderPiExternalRoots();
  renderPiShellWarning();
  const invariants = $("pi-perm-invariants");
  invariants.replaceChildren();
  for (const text of (piPermCurrent && piPermCurrent.fixed_invariants) || []) {
    invariants.append(node("p", text, "hint"));
  }
  if (piPermCurrent) {
    $("pi-perm-revision").textContent =
      `Revision ${String(piPermCurrent.policy_revision || "").slice(0, 12)} · ${piPermCurrent.session_note || "Applies to new Pi sessions only."}`;
  }
}
async function loadPiPermissions() {
  const data = await api("/api/runtimes/pi/permission-policy");
  piPermCurrent = data;
  piPermDraft = piPermDraftFrom(data.policy);
  renderPiPermissions();
}
async function openPiPermissions() {
  try {
    await loadPiPermissions();
  } catch (e) { message(e.message); return; }
  const dialog = $("pi-permissions-dialog");
  if (dialog && typeof dialog.showModal === "function") dialog.showModal();
  else message("This browser does not support the permission management dialog.");
}
function piPermDangerSummary(draft, current) {
  const lines = [];
  const cur = current ? current.policy : null;
  if (cur) {
    if (!cur.enabled && draft.enabled) lines.push("Enable writable tools (edit/write) for NEW Pi sessions.");
    if (cur.enabled && !draft.enabled) lines.push("Disable writable tools; NEW Pi sessions become read-only.");
    for (const tool of PI_PERM_TOOLS) {
      if (draft.tools[tool] !== cur.tools[tool]) {
        lines.push(`Tool ${tool}: ${cur.tools[tool]} -> ${draft.tools[tool]}.`);
      }
    }
    const removed = (cur.protected_patterns || []).filter((p) => !draft.protected_patterns.includes(p));
    for (const p of removed) lines.push(`Remove protected pattern: ${p}.`);
    const added = draft.protected_patterns.filter((p) => !(cur.protected_patterns || []).includes(p));
    for (const p of added) lines.push(`Add protected pattern: ${p}.`);
    if (Boolean(cur.allow_session_always) !== Boolean(draft.allow_session_always)) {
      lines.push(draft.allow_session_always
        ? "Offer “Always allow exact target” again."
        : "Stop offering “Always allow exact target” (once/reject only).");
    }
    const curShell = cur.shell_mode || "deny";
    if ((draft.shell_mode || "deny") !== curShell) {
      lines.push(`Shell execution: ${curShell} -> ${draft.shell_mode}.` +
        (draft.shell_mode === "allow"
          ? " WARNING: Allow runs with native user authority and can bypass file scope."
          : draft.shell_mode === "ask" ? " Each bash invocation will pause for approval." : ""));
    }
    const curExternal = cur.external_access || {default_mode: "deny", roots: []};
    if (draft.external_default_mode !== curExternal.default_mode) {
      lines.push(`External default: ${curExternal.default_mode} -> ${draft.external_default_mode}.` +
        (draft.external_default_mode !== "deny" ? " WARNING: broadens outside-workspace access." : ""));
    }
    const curRoots = curExternal.roots || [];
    const sameRoots = curRoots.length === draft.external_roots.length &&
      curRoots.every((r, i) => r.path === draft.external_roots[i].path &&
        r.mode === draft.external_roots[i].mode);
    if (!sameRoots) {
      lines.push(`External roots: ${curRoots.length} -> ${draft.external_roots.length} configured root(s). ` +
        draft.external_roots.map((r) => `${r.path || "(empty path)"} (${r.mode})`).join("; "));
      if (draft.external_roots.some((r) => r.path === "/")) {
        lines.push("WARNING: root “/” exposes the entire host filesystem.");
      }
    }
  }
  return lines;
}
async function savePiPermissions() {
  if (!piPermDraft) return;
  const draft = piPermDraft;
  draft.enabled = $("pi-perm-enabled").checked;
  draft.allow_session_always = $("pi-perm-always").checked;
  draft.protected_patterns = $("pi-perm-protected").value.split("\n").map((x) => x.trim()).filter(Boolean);
  draft.protected_template_exceptions = $("pi-perm-exceptions").value.split("\n").map((x) => x.trim()).filter(Boolean);
  for (const tool of PI_PERM_TOOLS) {
    const select = document.querySelector(`[data-pi-perm-tool="${tool}"]`);
    if (select && select.value) draft.tools[tool] = select.value;
  }
  const defaultSelect = $("pi-perm-external-default");
  if (defaultSelect && defaultSelect.value) draft.external_default_mode = defaultSelect.value;
  const shellSelect = $("pi-perm-shell");
  if (shellSelect && shellSelect.value) draft.shell_mode = shellSelect.value;
  draft.external_roots = draft.external_roots
    .map((r) => ({path: String(r.path || "").trim(), mode: r.mode}))
    .filter((r) => r.path);
  const summary = piPermDangerSummary(draft, piPermCurrent);
  const warning = "Pi runs natively with your macOS user authority; this is not a sandbox.\n" +
    "Policy changes apply to NEW Pi sessions only; active sessions keep their snapshot.\n" +
    (summary.length ? "Changes:\n- " + summary.join("\n- ") : "No changes compared to the current policy.");
  if (!confirm(`Save Pi permission policy?\n\n${warning}`)) return;
  const body = {
    version: 3,
    write_tools_enabled: draft.enabled,
    tools: draft.tools,
    protected_patterns: draft.protected_patterns,
    protected_template_exceptions: draft.protected_template_exceptions,
    allow_session_always: draft.allow_session_always,
    external_access: {
      default_mode: draft.external_default_mode,
      roots: draft.external_roots,
    },
    shell_mode: draft.shell_mode || "deny",
  };
  const saved = await api("/api/runtimes/pi/permission-policy", "POST", body);
  piPermCurrent = saved;
  piPermDraft = piPermDraftFrom(saved.policy);
  renderPiPermissions();
  const dialog = $("pi-permissions-dialog");
  if (dialog && typeof dialog.close === "function") dialog.close();
  await refresh();
  message(`Pi permission policy saved (rev ${String(saved.policy_revision || "").slice(0, 12)}). New Pi sessions only.`);
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

async function openModels(runtime) {
  modelRuntime = runtime || "opencode";
  try {
    await loadModels();
  } catch (e) { message(e.message); }
  const policies = settings.runtime_policies || {};
  const policy = modelRuntime === "opencode"
    ? (settings.model_policy || {enabled: [], default: null})
    : (policies[modelRuntime] || {enabled: [], default: null});
  draftEnabled = new Set(policy.enabled || []);
  draftDefault = policy.default || null;
  if ($("model-filter")) $("model-filter").value = "";
  $("models-runtime-eyebrow").textContent = modelRuntime === "opencode" ? "OpenCode runtime" : "Pi runtime";
  $("models-hint").textContent = modelRuntime === "opencode"
    ? "Global discovery: identical for every workspace."
    : "Workspace-bound discovery: models are listed through the selected workspace. The saved policy stays runtime-global.";
  const discoveryRow = $("discovery-workspace-row");
  if (modelRuntime === "opencode") {
    discoveryRow.hidden = true;
  } else {
    discoveryRow.hidden = false;
    renderDiscoveryWorkspaces();
  }
  $("load-models").textContent = modelRuntime === "opencode" ? "Refresh global models" : "Refresh Pi models";
  renderModelPolicy();
  const dialog = $("models-dialog");
  if (dialog && typeof dialog.showModal === "function") dialog.showModal();
  else message("This browser does not support the model management dialog.");
}

function renderDiscoveryWorkspaces() {
  const select = $("discovery-workspace");
  select.replaceChildren();
  const enabled = allWorkspaces.filter((w) => w.enabled);
  if (currentWorkspace && enabled.some((w) => w.id === currentWorkspace.id)) discoveryWorkspaceId = currentWorkspace.id;
  if (!discoveryWorkspaceId || !enabled.some((w) => w.id === discoveryWorkspaceId)) discoveryWorkspaceId = enabled.length ? enabled[0].id : null;
  if (!enabled.length) {
    const option = node("option", "No enabled workspace"); option.value = "";
    select.append(option);
  }
  for (const w of enabled) {
    const option = node("option", w.name); option.value = w.id;
    select.append(option);
  }
  select.value = discoveryWorkspaceId || "";
  if (!select.value) discoveryWorkspaceId = null;
}

async function loadModels() {
  let url = `/api/runtimes/${modelRuntime}/models?limit=100`;
  if (modelRuntime !== "opencode") {
    const wsId = discoveryWorkspaceId || (currentWorkspace && currentWorkspace.id);
    if (!wsId) throw new Error("Select an enabled discovery workspace for Pi models.");
    url += `&workspace_id=${encodeURIComponent(wsId)}`;
  }
  const data = await api(url);
  globalModels = data.models || [];
  if (data.policy) {
    if (modelRuntime === "opencode") settings.model_policy = data.policy;
    else settings.runtime_policies = {...(settings.runtime_policies || {}), [modelRuntime]: data.policy};
  }
  renderModelPolicy();
  renderRuntime(config);
  if (!globalModels.length) message(modelRuntime === "opencode"
    ? "No models are available in the current global OpenCode runtime."
    : "No models are available through the selected Pi discovery workspace.");
}

async function saveModelPolicy() {
  const enabled = globalModels.filter((m) => draftEnabled.has(m.selector)).map((m) => m.selector);
  const def = $("policy-default").value || null;
  if (!enabled.length) { message("Enable at least one model before saving."); return; }
  if (!def || !draftEnabled.has(def)) { message("Choose the mandatory default among the enabled models."); return; }
  const body = {enabled, default: def};
  if (modelRuntime !== "opencode") {
    const wsId = $("discovery-workspace").value || discoveryWorkspaceId;
    if (!wsId) { message("Select an enabled discovery workspace before saving the Pi policy."); return; }
    body.workspace_id = wsId;
  }
  if (!confirm(`Save ${modelRuntime} model policy with ${enabled.length} enabled model(s) and mandatory default ${def}? Runs without a model use the default; explicit models must be enabled.`)) return;
  const saved = await api(`/api/runtimes/${modelRuntime}/model-policy`, "POST", body);
  if (modelRuntime === "opencode") settings.model_policy = saved;
  else settings.runtime_policies = {...(settings.runtime_policies || {}), [modelRuntime]: saved};
  draftEnabled = new Set(saved.enabled || []);
  draftDefault = saved.default || null;
  const dialog = $("models-dialog");
  if (dialog && typeof dialog.close === "function") dialog.close();
  await loadModels();
  message(`Model policy saved. Default: ${def}.`);
}

function auditLabel(run) {
  const audit = run.execution_audit;
  if (!audit || run.runtime !== "pi") return "audit not-recorded";
  const counts = audit.counts || {};
  const base = `audit ${audit.status || "not-recorded"}`;
  const parts = [`${counts.total || 0} exec`, `${counts.failed || 0} failed`, `${counts.shell || 0} shell`];
  return `${base} · ${parts.join(" · ")}`;
}
async function executionView(runId) {
  const data = await api(`/api/runs/${runId}/executions?${new URLSearchParams({offset: "0", limit: "50"})}`);
  const container = node("div");
  container.append(node("p", `Runtime ${(data.runtime || "")} · ${data.executions.length} shown (bounded summaries; no output bodies).`, "muted"));
  for (const ex of data.executions || []) {
    const line = `${ex.sequence || ex.seq} · ${ex.tool} · ${ex.state}${ex.is_error ? " · ERROR" : ""} · ${ex.target_preview || "—"} · ${ex.permission_effect || ""}${ex.permission_decision ? "/" + ex.permission_decision : ""}${ex.truncated ? " · truncated" : ""}`;
    const row = node("div", undefined, "job");
    row.append(node("div", line, "path"));
    const detailBtn = button("Execution detail", async () => {
      const detail = await api(`/api/runs/${runId}/executions/${encodeURIComponent(ex.execution_id)}`);
      // Safe DOM/textContent only: bounded/truncated potentially
      // sensitive local output is rendered as text, never HTML.
      show(`Execution ${ex.execution_id} (bounded, may be sensitive)`, detail);
    });
    row.append(detailBtn);
    container.append(row);
  }
  if (!(data.executions || []).length) container.append(node("p", "No executions recorded.", "muted"));
  $("output-title").textContent = `Execution history for ${runId}`;
  $("output").textContent = ""; $("output").append(container); $("output-panel").hidden = false; $("output-panel").scrollIntoView({behavior: "smooth", block: "center"});
}
function runRow(ws, run) {
  const row = node("div", undefined, "job");
  const runtimeLabel = run.runtime || "opencode";
  const title = node("h3", `${run.state} · ${run.model || "Default model"} · ${runtimeLabel}`);
  row.append(title, node("div", `Run ${run.run_id} · Runtime ${runtimeLabel} · Session ${run.session_id || "—"} · Job ${run.job_id} · Request ${run.request_id}`, "path"));
  if (run.session_reused) row.append(node("div", `Reused ${runtimeLabel} session · Continued from ${run.continue_from_run_id || run.parent_run_id || "—"}`, "muted"));
  if (run.execution_audit || runtimeLabel === "pi") row.append(node("div", auditLabel(run), "muted"));
  const times = [`created ${run.created ? new Date(run.created).toLocaleString() : "—"}`];
  if (run.started) times.push(`started ${new Date(run.started).toLocaleString()}`);
  if (run.finished) times.push(`finished ${new Date(run.finished).toLocaleString()}`);
  if (run.duration_seconds !== null && run.duration_seconds !== undefined) times.push(`${run.duration_seconds}s`);
  times.push(`notification ${(run.notification && run.notification.status) || "none"}`);
  row.append(node("div", times.join(" · "), "muted"));
  const actions = node("div", undefined, "actions");
  actions.append(button("View session", async () => { const data = await api(`/api/runs/${run.run_id}/session`); const lines = (data.transcript || []).map(m => `[${m.role}] ${m.error ? "ERROR " + m.error + "\n" : ""}${m.text || ""}${m.tools && m.tools.length ? "\ntools: " + m.tools.join(", ") : ""}`).join("\n\n"); show(`Session ${run.session_id || ""} (escaped, bounded)`, lines || "No messages yet."); }));
  actions.append(button("Run details", async () => { const data = await api(`/api/runs/${run.run_id}`); show(`Run ${run.run_id}`, data); }));
  actions.append(button("Execution history", () => executionView(run.run_id)));
  if (run.active) actions.append(button("Stop", () => stopRun(run.run_id), "danger"));
  if (run.pending_request_count > 0) actions.append(button(`Requests (${run.pending_request_count})`, () => requestView(ws, run.run_id)));
  row.append(actions);
  return row;
}

async function stopRun(runId) {
  if (!confirm("Interrupt only this recorded session? No process is killed directly.")) return;
  const result = await api(`/api/runs/${runId}/stop`, "POST");
  message(result.cancelled ? "Session interrupted; run marked cancelled." : `Run state unchanged (${result.state}).`);
  await refresh(); if (currentWorkspace) await jobs(currentWorkspace);
}

function sessionRow(run) {
  const row = node("tr");
  row.append(node("td", run.state || "—"));
  row.append(node("td", run.runtime || "opencode"));
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
  actions.append(button("Executions", () => executionView(run.run_id)));
  if (run.active) actions.append(button("Stop", () => stopRun(run.run_id), "danger"));
  if (pending > 0) actions.append(button(`Requests (${pending})`, async () => { const data = await api(`/api/runs/${run.run_id}`); show(`Run ${run.run_id}`, data); }));
  actionsCell.append(actions);
  row.append(actionsCell);
  return row;
}

async function loadSessions(reset) {
  if (reset) { sessionsOffset = 0; $("sessions").replaceChildren(); }
  const data = await api(`/api/sessions?${new URLSearchParams({offset: String(sessionsOffset), limit: String(SESSIONS_PAGE)})}`);
  $("sessions-count").textContent = `${data.runs.length} shown`;
  if (reset && !data.runs.length) { const empty = node("tr"); const cell = node("td", "No Bridge-owned agent sessions yet.", "muted"); cell.colSpan = 10; empty.append(cell); $("sessions").append(empty); }
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
    ? "Approve the runtime's EXACT proposed pattern for this session? Bridge never broadens it. Review the scope above first."
    : decision === "once" ? "Approve only this request?" : "Reject this request?";
  if (!confirm(warning)) return;
  const result = await api(`/api/runs/${runId}/requests/${requestId}`, "POST", {decision});
  message(`Permission ${result.decision} recorded. Run state: ${result.run_state}. Same session resumes.`);
  await refresh(); if (currentWorkspace) await jobs(currentWorkspace);
}

async function jobs(ws) {
  currentWorkspace = ws;
  const [data, runs] = await Promise.all([api(`/api/workspaces/${ws.id}/jobs`), api(`/api/workspaces/${ws.id}/runs`)]);
  navigate("jobs-panel");
  $("jobs-title").textContent = `${ws.name} · Handoffs & runs`; $("jobs-panel").hidden = false; $("jobs").replaceChildren();
  $("jobs").append(node("h3", "Handoffs"));
  if (!data.handoffs.length) $("jobs").append(node("p", "No handoffs yet. Ask ChatGPT to inspect the project and prepare a handoff.", "muted"));
  for (const j of data.handoffs) {
    const row = node("div", undefined, "job"); row.append(node("h3", `${j.title} · ${j.legacy_handoff ? "legacy" : j.state === "prepared" ? "published" : j.state}`), node("div", j.path, "path"));
    const actions = node("div", undefined, "actions");
    actions.append(button("Copy agent prompt", async () => { try { await navigator.clipboard.writeText(j.copy_prompt); message("Handoff prompt copied. Paste it into the agent yourself."); } catch { show("Copy this prompt", j.copy_prompt); } }));
    for (const [label, doc] of [["Plan", "TASK.md"], ["Context", "CONTEXT.md"], ["Acceptance", "ACCEPTANCE.md"]]) {
      actions.append(button(label, async () => { const result = await api(`/api/workspaces/${ws.id}/document?${new URLSearchParams({job_id:j.id, document:doc})}`); show(`${j.title} · ${label}`, result); }));
    }
    row.append(actions); $("jobs").append(row);
  }
  $("jobs").append(node("h3", "Runs"));
  if (!runs.runs.length) $("jobs").append(node("p", "No agent runs yet. Enable agent execution for this workspace, then ask ChatGPT to start the prepared handoff.", "muted"));
  for (const run of runs.runs) $("jobs").append(runRow(ws, run));
  if (data.next_offset !== null) $("jobs").append(node("p", "Showing the most recent 40 handoffs. Use the paginated MCP tool for older entries.", "muted"));
}

async function refresh() {
  const [data, history, status, currentSettings] = await Promise.all([api("/api/workspaces"), api("/api/events"), api("/api/status"), api("/api/settings")]);
  config = status; bridgeState = status.bridge;
  settings = {...currentSettings, runtime_policies: status.runtime_policies || {}};
  allWorkspaces = data.workspaces || [];
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
    row.dataset.search = `${ws.name} ${ws.root}`.toLowerCase();
    row.append(title, node("div", ws.root, "path"), node("div", `Write: ${ws.write_scope} · Agent: ${ws.agent_enabled ? "enabled" : "disabled"}`, "workspace-meta"));
    const actions = node("div", undefined, "actions");
    // Policy controls first: workspace access immediately followed by the agent toggle.
    actions.append(button(ws.enabled ? "Disable access" : "Enable access", () => { if (ws.enabled || confirm("Expose this mapping to every chat using the shared bridge connection?")) return manage(ws.id, ws.enabled ? "disable" : "enable"); }, ws.enabled ? "danger" : "secondary"));
    actions.append(button(ws.agent_enabled ? "Disable agent" : "Enable agent", async () => {
      const enabled = !ws.agent_enabled;
      const warning = enabled
        ? "Enable agent execution for this workspace? Any holder of the shared bridge credential can then start bounded runs for prepared handoffs here. Runs can read/write files under the workspace with the host runtime server's authority and may request permissions. This is independent of write permission and default OFF."
        : "Disable agent execution for this workspace? Existing runs are not stopped automatically.";
      if (!confirm(warning)) return;
      await api(`/api/workspaces/${ws.id}`, "POST", {operation: "set_agent_enabled", agent_enabled: enabled});
      await refresh(); message(`Agent execution ${enabled ? "enabled" : "disabled"}. It is independent of write scope.`);
    }, ws.agent_enabled ? "danger" : "secondary"));
    actions.append(button("Handoffs & runs", () => jobs(ws)));
    const details = node("details"); details.append(node("summary", "Settings"));
    details.append(node("p", "Built-in exclusions stay enforced. Extra patterns apply to future browsing.", "muted"));
    details.append(button("Copy workspace ID", async () => { try { await navigator.clipboard.writeText(ws.id); message("Workspace ID copied."); } catch { show("Workspace ID", ws.id); } }));
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
  filterWorkspaces();
  const names = Object.fromEntries(data.workspaces.map(w => [w.id, w.name])); $("events").replaceChildren();
  for (const e of history.events) { const tr = node("tr"); for (const value of [new Date(e.at).toLocaleString(), names[e.workspace] || "Admin", e.action, e.outcome]) tr.append(node("td", value)); $("events").append(tr); }
  if (!history.events.length) {
    const row = node("tr");
    const cell = node("td", "No activity yet. Access and configuration changes will appear here.", "muted");
    cell.colSpan = 4; row.append(cell); $("events").append(row);
  }
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
    $("status-line").textContent = `v${config.version} · ${config.listen_mode === "docker-published-loopback" ? "Docker loopback" : "Loopback only"}`;
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
    $("status-line").textContent = `v${config.version} · ${config.listen_mode === "docker-published-loopback" ? "Docker loopback" : "Loopback only"}`;
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
$("open-models").onclick = () => openModels("opencode").catch(e=>message(e.message));
$("open-pi-models").onclick = () => openModels("pi").catch(e=>message(e.message));
$("open-pi-permissions").onclick = () => openPiPermissions().catch(e=>message(e.message));
$("pi-perm-reload").onclick = () => loadPiPermissions().catch(e=>message(e.message));
$("pi-perm-restore").onclick = () => {
  if (!piPermCurrent) return;
  piPermDraft = piPermDraftFrom({
    version: 2, write_tools_enabled: false,
    tools: {read: "allow", grep: "allow", find: "allow", ls: "allow", edit: "ask", write: "ask"},
    protected_patterns: [".git/**", ".env", ".env.*", ".workspace-handoff/**"],
    protected_template_exceptions: [".env.example", ".env.sample", ".env.template"],
    allow_session_always: true,
    external_access: {default_mode: "deny", roots: []},
  });
  renderPiPermissions();
  message("Safe defaults loaded in the draft (external access denied, no roots). Press “Save permission policy” to apply.");
};
$("pi-perm-add-root").onclick = () => {
  if (!piPermDraft) return;
  if (piPermDraft.external_roots.length >= 32) {
    message("At most 32 external roots are allowed.");
    return;
  }
  piPermDraft.external_roots.push({path: "", mode: "ask"});
  renderPiExternalRoots();
};
$("pi-perm-external-default").onchange = (event) => {
  if (piPermDraft) {
    piPermDraft.external_default_mode = event.target.value;
    renderPiExternalWarning();
  }
};
$("pi-perm-save").onclick = () => savePiPermissions().catch(e=>message(e.message));
$("pi-perm-cancel").onclick = () => { const dialog = $("pi-permissions-dialog"); if (dialog && typeof dialog.close === "function") dialog.close(); };
$("pi-perm-enabled").onchange = () => { if (piPermDraft) { piPermDraft.enabled = $("pi-perm-enabled").checked; renderPiPermissions(); } };
$("pi-perm-always").onchange = () => { if (piPermDraft) piPermDraft.allow_session_always = $("pi-perm-always").checked; };
$("load-models").onclick = () => loadModels().catch(e=>message(e.message));
$("save-policy").onclick = () => saveModelPolicy().catch(e=>message(e.message));
$("cancel-policy").onclick = () => { const dialog = $("models-dialog"); if (dialog && typeof dialog.close === "function") dialog.close(); };
$("model-filter").oninput = () => renderModelPolicy();
$("discovery-workspace").onchange = (event) => { discoveryWorkspaceId = event.target.value || null; loadModels().catch(e=>message(e.message)); };
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

// Section navigation keeps policy and activity views separate from daily project work.
const views = {
  "workspaces-panel": ["Workspaces", "Manage the projects your agents can access."],
  "jobs-panel": ["Handoffs & runs", "Follow work from a prepared handoff to its result."],
  "sessions-panel": ["Agent sessions", "Review progress and respond when an agent needs you."],
  "system-panel": ["System", "Manage runtime connections, models, and permissions."],
  "activity-panel": ["Activity", "A record of access and configuration changes."]
};
function navigate(id, focus = false) {
  if (!views[id]) id = "workspaces-panel";
  for (const key of Object.keys(views)) $(key).hidden = key !== id;
  document.querySelectorAll(".side-nav a").forEach(link => {
    if (link.hash === `#${id}`) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  $("page-title").textContent = views[id][0];
  $("status-dup").textContent = views[id][1];
  if (location.hash !== `#${id}`) history.replaceState(null, "", `#${id}`);
  if (focus) $("page-title").focus();
}
document.querySelectorAll(".side-nav a").forEach(link => {
  link.onclick = event => { event.preventDefault(); navigate(link.hash.slice(1), true); };
});
window.addEventListener("hashchange", () => navigate(location.hash.slice(1)));
$("choose-workspace").onclick = () => navigate("workspaces-panel", true);
$("add-workspace").onclick = () => {
  $("add-workspace-drawer").open = true;
  $("add-form").elements.name.focus();
};
function filterWorkspaces() {
  const query = $("workspace-search").value.trim().toLowerCase();
  let matches = 0;
  document.querySelectorAll(".workspace").forEach(row => {
    row.hidden = !row.dataset.search.includes(query);
    if (!row.hidden) matches++;
  });
  $("search-empty").hidden = !query || matches > 0;
}
$("workspace-search").oninput = filterWorkspaces;
navigate(location.hash.slice(1));

document.addEventListener("keydown", event => {
  if (event.key === "Escape" && !document.querySelector("dialog[open]")) {
    $("close-output").click();
  }
});
