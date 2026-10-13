// Private, durable append journal for bounded execution audit snapshots.
// The file contains only normalized record fields; it never receives Pi
// events or arbitrary input/result objects.
import fs from "node:fs";
import path from "node:path";
import { createHash } from "node:crypto";
import { normalizeExecutionRecord, normalizeExecutionSnapshot } from "./executions.mjs";

const MAX_RECORDS = 500;
const MAX_RECORD_BYTES = 256 * 1024;
const COMPACT_BYTES = 4 * 1024 * 1024;
const MAX_LOG_BYTES = MAX_RECORDS * MAX_RECORD_BYTES + COMPACT_BYTES + 1024 * 1024;
const COMPACT_FRAMES = 64;
const ROOT_NAME = "execution-journals";
const STORE_ERROR = "execution journal unavailable";

function fail() { throw new Error(STORE_ERROR); }
function hasExactKeys(value, keys) {
  return value && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key));
}
function canonicalBinding(binding) {
  if (!binding || typeof binding !== "object" || Array.isArray(binding)
    || Object.keys(binding).length !== 4
    || typeof binding.sessionId !== "string" || !binding.sessionId || binding.sessionId.length > 200
    || typeof binding.directory !== "string" || !path.isAbsolute(binding.directory) || binding.directory.length > 4096
    || typeof binding.policyRevision !== "string" || !binding.policyRevision || binding.policyRevision.length > 200
    || typeof binding.extensionRevision !== "string" || !binding.extensionRevision || binding.extensionRevision.length > 200) fail();
  return JSON.stringify({ sessionId: binding.sessionId, directory: binding.directory,
    policyRevision: binding.policyRevision, extensionRevision: binding.extensionRevision });
}

function assertRealDirectory(directory, create = false) {
  try {
    if (create) fs.mkdirSync(directory, { mode: 0o700 });
    const st = fs.lstatSync(directory);
    if (!st.isDirectory() || st.isSymbolicLink()
      || (path.basename(directory) === ROOT_NAME && (st.mode & 0o077) !== 0)) fail();
  } catch { fail(); }
}

function validateRecord(record) {
  try {
    const safe = normalizeExecutionRecord(record);
    if (Buffer.byteLength(JSON.stringify(safe)) > MAX_RECORD_BYTES) fail();
    return safe;
  } catch { fail(); }
}

function validateSnapshot(snapshot) {
  try {
    const safe = normalizeExecutionSnapshot(snapshot);
    if (safe.records.some((record) => Buffer.byteLength(JSON.stringify(record)) > MAX_RECORD_BYTES)) fail();
    return safe;
  } catch { fail(); }
}

function fsyncDirectory(directory) {
  const fd = fs.openSync(directory, fs.constants.O_RDONLY);
  try { fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
}

function writeAll(fd, buffer) {
  let offset = 0;
  while (offset < buffer.length) {
    const written = fs.writeSync(fd, buffer, offset, buffer.length - offset);
    if (!Number.isInteger(written) || written <= 0) fail();
    offset += written;
  }
}

function sameJson(a, b) { return JSON.stringify(a) === JSON.stringify(b); }

export class ExecutionStore {
  constructor({ agentDir, binding } = {}) {
    if (typeof agentDir !== "string" || !path.isAbsolute(agentDir)) fail();
    this.agentDir = path.resolve(agentDir);
    this.bindingText = canonicalBinding(binding);
    this.binding = JSON.parse(this.bindingText);
    this.bindingHash = createHash("sha256").update(this.bindingText).digest("hex");
    this.root = path.join(this.agentDir, ROOT_NAME);
    this.file = path.join(this.root, `${this.bindingHash}.jsonl`);
    this.failed = false;
    this.loaded = false;
    this.frames = 0;
    this.last = null;
  }

  _healthy() { if (this.failed) fail(); }
  _latch() { this.failed = true; fail(); }

  _prepare() {
    this._healthy();
    try {
      try { fs.mkdirSync(this.agentDir, { recursive: true, mode: 0o700 }); } catch { fail(); }
      assertRealDirectory(this.agentDir);
      try { fs.mkdirSync(this.root, { mode: 0o700 }); } catch (e) { if (e.code !== "EEXIST") throw e; }
      assertRealDirectory(this.root);
    } catch { this._latch(); }
  }

  _readFile() {
    this._prepare();
    try {
      let fd;
      try { fd = fs.openSync(this.file, fs.constants.O_RDWR | fs.constants.O_NOFOLLOW); }
      catch (e) { if (e.code === "ENOENT") return null; throw e; }
      try {
        const st = fs.fstatSync(fd);
        if (!st.isFile() || (st.mode & 0o077) !== 0 || st.size > MAX_LOG_BYTES) fail();
        return { fd, bytes: fs.readFileSync(fd) };
      } catch (e) { fs.closeSync(fd); throw e; }
    } catch { this._latch(); }
  }

  load() {
    this._healthy();
    try {
      const source = this._readFile();
      if (source === null) { this.loaded = true; this.last = null; return null; }
      const { fd, bytes } = source;
      try {
        let pos = 0;
        const commits = [];
        while (pos < bytes.length) {
          const nl = bytes.indexOf(0x0a, pos);
          if (nl < 0) break; // only an incomplete final frame is recoverable
          if (nl === pos) fail();
          let commit;
          try { commit = JSON.parse(bytes.subarray(pos, nl).toString("utf8")); } catch { fail(); }
          commits.push(commit);
          pos = nl + 1;
        }
        let snapshot = null;
        for (const commit of commits) snapshot = this._apply(snapshot, commit);
        // Discard only the unterminated tail. Complete malformed frames fail above.
        if (pos < bytes.length) {
          fs.ftruncateSync(fd, pos);
          fs.fsyncSync(fd);
        }
        this.frames = commits.length;
        this.last = snapshot;
        this.loaded = true;
        return snapshot;
      } finally { fs.closeSync(fd); }
    } catch { this._latch(); }
  }

  _apply(previous, commit) {
    if (!hasExactKeys(commit, ["version", "kind", "binding", "counters", "upserts", "removals"])
      || commit.version !== 1 || commit.binding !== this.bindingText
      || !["delta", "checkpoint"].includes(commit.kind)
      || !hasExactKeys(commit.counters, ["maxRecords", "startOrder", "updateSeq", "evictedThrough"])
      || !Number.isSafeInteger(commit.counters.maxRecords)
      || !Number.isSafeInteger(commit.counters.startOrder) || !Number.isSafeInteger(commit.counters.updateSeq)
      || !Number.isSafeInteger(commit.counters.evictedThrough)
      || !Array.isArray(commit.upserts) || !Array.isArray(commit.removals)) fail();
    // Atomic compaction replaces the file, so a checkpoint can only be
    // its first frame. An appended checkpoint must not reset coverage.
    if (commit.kind === "checkpoint" && previous !== null) fail();
    let records = new Map();
    if (commit.kind === "delta") {
      if (!previous) fail();
      if (commit.counters.maxRecords !== previous.maxRecords
        || commit.counters.startOrder < previous.startOrder
        || commit.counters.updateSeq <= previous.updateSeq
        || commit.counters.updateSeq - previous.updateSeq !== commit.upserts.length
        || commit.counters.evictedThrough < previous.evictedThrough) fail();
      records = new Map(previous.records.map((r) => [r.tool_call_id, r]));
    }
    const removed = new Set();
    for (const id of commit.removals) {
      if (typeof id !== "string" || !id || !records.has(id)) fail();
      if (removed.has(id)) fail();
      removed.add(id);
      records.delete(id);
    }
    const updated = new Set();
    for (const raw of commit.upserts) {
      const safe = validateRecord(raw);
      if (updated.has(safe.tool_call_id) || removed.has(safe.tool_call_id)) fail();
      updated.add(safe.tool_call_id);
      const prior = records.get(safe.tool_call_id);
      if (prior && (safe.update_seq <= prior.update_seq || prior.state !== "started"
        || safe.start_seq !== prior.start_seq || safe.start_order !== prior.start_order
        || safe.tool !== prior.tool || safe.started_at !== prior.started_at
        || safe.permission_effect !== prior.permission_effect
        || !sameJson(safe.input_summary, prior.input_summary))) fail();
      if (records.has(safe.tool_call_id)) records.delete(safe.tool_call_id);
      records.set(safe.tool_call_id, safe);
    }
    if (commit.kind === "delta") {
      for (let i = 0; i < commit.upserts.length; i += 1) {
        const record = normalizeExecutionRecord(commit.upserts[i]);
        if (record.update_seq !== previous.updateSeq + i + 1) fail();
      }
    }
    const snapshot = validateSnapshot({ version: 1, ...commit.counters, records: [...records.values()] });
    if (commit.kind === "delta") {
      const priorIds = new Set(previous.records.map((r) => r.tool_call_id));
      const added = snapshot.records.filter((r) => !priorIds.has(r.tool_call_id));
      for (const removedId of removed) {
        const prior = previous.records.find((r) => r.tool_call_id === removedId);
        if (!prior || prior.update_seq > snapshot.evictedThrough) fail();
      }
      if (added.length > 1 || (added.length === 1 && (added[0].state !== "started"
        || added[0].update_seq !== added[0].start_seq
        || added[0].start_seq <= previous.updateSeq
        || added[0].start_order !== previous.startOrder + 1
        || snapshot.startOrder !== added[0].start_order))
        || (added.length === 0 && snapshot.startOrder !== previous.startOrder)) fail();
    }
    if (commit.kind === "checkpoint" && (commit.removals.length !== 0
      || snapshot.records.length !== commit.upserts.length)) fail();
    return snapshot;
  }

  save(snapshot) {
    this._healthy();
    try {
      const safe = validateSnapshot(snapshot);
      if (!this.loaded) this.load();
      this._prepare();
      if (this.last && sameJson(safe, this.last)) return;
      const oldMap = new Map((this.last?.records || []).map((r) => [r.tool_call_id, r.update_seq]));
      const nextMap = new Map(safe.records.map((r) => [r.tool_call_id, r]));
      const removals = [...oldMap.keys()].filter((id) => !nextMap.has(id));
      const upserts = safe.records.filter((r) => oldMap.get(r.tool_call_id) !== r.update_seq);
      const counters = { maxRecords: safe.maxRecords, startOrder: safe.startOrder,
        updateSeq: safe.updateSeq, evictedThrough: safe.evictedThrough };
      if (!this.last) {
        this._apply(null, { version: 1, kind: "checkpoint", binding: this.bindingText,
          counters, upserts: safe.records, removals: [] });
        this._compact(safe);
        return;
      }
      const commit = { version: 1, kind: "delta", binding: this.bindingText, counters, upserts, removals };
      this._apply(this.last, commit);
      const frame = Buffer.from(`${JSON.stringify(commit)}\n`, "utf8");
      if (frame.length > MAX_LOG_BYTES) fail();
      let fd;
      try { fd = fs.openSync(this.file, fs.constants.O_WRONLY | fs.constants.O_APPEND | fs.constants.O_NOFOLLOW); }
      catch { fail(); }
      const st = fs.fstatSync(fd);
      if (!st.isFile() || (st.mode & 0o077) !== 0) { fs.closeSync(fd); fail(); }
      if (st.size + frame.length > MAX_LOG_BYTES) {
        fs.closeSync(fd);
        this._compact(safe);
      } else {
        try { writeAll(fd, frame); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
        fsyncDirectory(this.root);
        this.frames += 1;
      }
      this.last = safe;
      if (this.frames > COMPACT_FRAMES || fs.statSync(this.file).size > COMPACT_BYTES) this._compact(safe);
    } catch { this._latch(); }
  }

  _compact(snapshot) {
    this._prepare();
    const commit = { version: 1, kind: "checkpoint", binding: this.bindingText,
      counters: { maxRecords: snapshot.maxRecords, startOrder: snapshot.startOrder,
        updateSeq: snapshot.updateSeq, evictedThrough: snapshot.evictedThrough },
      upserts: snapshot.records, removals: [] };
    const data = Buffer.from(`${JSON.stringify(commit)}\n`, "utf8");
    if (data.length > MAX_LOG_BYTES) fail();
    this._apply(null, commit);
    let existing;
    try { existing = fs.openSync(this.file, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW); }
    catch (e) { if (e.code !== "ENOENT") throw e; }
    if (existing !== undefined) {
      try {
        const st = fs.fstatSync(existing);
        if (!st.isFile() || (st.mode & 0o077) !== 0) fail();
      } finally { fs.closeSync(existing); }
    }
    const tmp = path.join(this.root, `.${this.bindingHash}.${process.pid}.${createHash("sha256").update(String(Date.now()) + Math.random()).digest("hex").slice(0, 12)}.tmp`);
    let fd;
    try {
      fd = fs.openSync(tmp, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_NOFOLLOW, 0o600);
      writeAll(fd, data); fs.fsyncSync(fd); fs.closeSync(fd); fd = undefined;
      fs.renameSync(tmp, this.file);
      fsyncDirectory(this.root);
      this.frames = 1;
      this.last = snapshot;
    } catch (e) {
      if (fd !== undefined) try { fs.closeSync(fd); } catch { /* noop */ }
      try { fs.unlinkSync(tmp); } catch { /* retain sanitized failure */ }
      throw e;
    }
  }
}
