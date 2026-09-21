// Project-root confinement for Pi sessions.
//
// Every requested session directory must realpath to an existing directory
// strictly under the canonical projects parent. Rejects filesystem roots,
// the home directory, outside paths, nonexistent paths, files, and symlink
// escapes. Stores canonical paths only.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export class PathError extends Error {
  constructor(message, code = "rejected") {
    super(message);
    this.name = "PathError";
    this.code = code;
    this.status = code === "not_found" ? 404 : 400;
  }
}

export function canonicalizeProjectsDir(raw) {
  const value = String(raw || "").trim();
  if (!value) throw new PathError("WB_PI_PROJECTS_DIR is not configured", "not_configured");
  const absolute = path.isAbsolute(value) ? path.normalize(value) : path.resolve(value);
  let real;
  try {
    real = fs.realpathSync(absolute);
  } catch {
    throw new PathError("Projects directory does not exist", "not_configured");
  }
  let stat;
  try {
    stat = fs.statSync(real);
  } catch {
    throw new PathError("Projects directory is not accessible", "not_configured");
  }
  if (!stat.isDirectory()) throw new PathError("Projects directory is not a directory", "not_configured");
  const parsed = path.parse(real);
  if (real === parsed.root) throw new PathError("Projects directory must not be a filesystem root", "rejected");
  return real;
}

function isWithin(parent, candidate) {
  const rel = path.relative(parent, candidate);
  return rel !== "" && !rel.startsWith("..") && !path.isAbsolute(rel);
}

export function resolveSessionDir(projectsRoot, requested, homeDir = os.homedir()) {
  const value = String(requested || "").trim();
  if (!value) throw new PathError("directory is required", "rejected");
  const home = path.normalize(homeDir || os.homedir());
  let absolute;
  if (value === "~" || value === "$HOME" || value === "${HOME}") {
    throw new PathError("directory must be a workspace under the projects parent", "rejected");
  } else if (value.startsWith("~/") || value.startsWith("$HOME/") || value.startsWith("${HOME}/")) {
    throw new PathError("directory must be a workspace under the projects parent", "rejected");
  } else if (path.isAbsolute(value)) {
    absolute = path.normalize(value);
  } else {
    absolute = path.resolve(projectsRoot, value);
  }
  const parsed = path.parse(absolute);
  if (absolute === parsed.root) throw new PathError("directory is not a workspace", "rejected");
  if (absolute === home) throw new PathError("directory must be a workspace under the projects parent", "rejected");
  let real;
  try {
    real = fs.realpathSync(absolute);
  } catch {
    throw new PathError("directory does not exist", "not_found");
  }
  let stat;
  try {
    stat = fs.statSync(real);
  } catch {
    throw new PathError("directory is not accessible", "not_found");
  }
  if (!stat.isDirectory()) throw new PathError("directory is not a directory", "rejected");
  if (real === projectsRoot) throw new PathError("directory must be a workspace under the projects parent", "rejected");
  if (!isWithin(projectsRoot, real)) {
    throw new PathError("directory is outside the projects parent", "rejected");
  }
  return real;
}
