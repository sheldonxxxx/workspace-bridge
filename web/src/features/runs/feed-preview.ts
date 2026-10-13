import type { Json } from "@/lib/api";
import type { ExecutionRecord } from "./api";

export function objectValue(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function outputPreviewText(value: unknown, depth = 0): string {
  if (depth > 5 || value == null) return "";
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (depth < 5 && (trimmed.startsWith("{") || trimmed.startsWith("["))) {
      try {
        const parsed = JSON.parse(trimmed) as unknown;
        const readable = outputPreviewText(parsed, depth + 1);
        if (readable) return readable;
      } catch {
        // Keep ordinary command output unchanged when it is not JSON.
      }
    }
    return value;
  }
  if (Array.isArray(value)) {
    return value
      .map((item) => outputPreviewText(item, depth + 1))
      .filter(Boolean)
      .join("\n");
  }
  if (typeof value !== "object") return String(value);

  const record = value as Record<string, unknown>;
  const previewKeys = [
    "output_preview",
    "outputPreview",
    "aggregatedOutput",
    "stdout",
    "output",
    "preview",
    "message",
    "text",
    "status",
    "content",
  ];
  for (const key of previewKeys) {
    if (record[key] !== undefined && record[key] !== value) {
      const preview = outputPreviewText(record[key], depth + 1);
      if (preview) return preview;
    }
  }

  const ignoredKeys = new Set([
    "is_error",
    "isError",
    "truncated",
    "output_bytes",
    "outputBytes",
    "preview_bytes",
    "previewBytes",
    "durationMs",
    "duration_ms",
  ]);
  return Object.entries(record)
    .filter(([key, item]) => !ignoredKeys.has(key) && item !== undefined)
    .map(([key, item]) => {
      const label = key
        .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
        .replace(/_/g, " ")
        .replace(/\b\w/g, (char) => char.toUpperCase());
      const text = outputPreviewText(item, depth + 1);
      return text ? `${label}: ${text}` : "";
    })
    .filter(Boolean)
    .join("\n");
}

export function executionInputPreview(
  execution: ExecutionRecord,
  activityDetails?: Json,
): string {
  const input = objectValue(activityDetails?.input);
  const details = objectValue(input.details);
  const filtered = Object.fromEntries(
    Object.entries(details).filter(
      ([key, value]) =>
        !/(?:_sha256|_bytes)$|^truncated$/i.test(key) && value !== undefined,
    ),
  );
  if (typeof filtered.command === "string") {
    const command = `$ ${filtered.command}`;
    return command.length > 1200 ? `${command.slice(0, 1200)}\n…` : command;
  }
  if (Object.keys(filtered).length) {
    const summary = JSON.stringify(filtered, null, 2);
    return summary.length > 1200 ? `${summary.slice(0, 1200)}\n…` : summary;
  }
  if (execution.input_preview) return execution.input_preview;
  return (
    execution.target_preview ||
    (typeof input.summary === "string" ? input.summary : "") ||
    "Input not recorded"
  );
}

export function executionOutputPreview(
  activityDetails?: Json,
  execution?: ExecutionRecord,
): {
  text: string;
  truncated: boolean;
} {
  const result = objectValue(activityDetails?.result);
  const preview =
    outputPreviewText(result) || outputPreviewText(execution?.output_preview);
  return {
    text: preview
      ? preview.slice(0, 900)
      : activityDetails
        ? "No output captured"
        : "Output preview unavailable",
    truncated:
      result.truncated === true ||
      execution?.output_truncated === true ||
      preview.length > 900,
  };
}
