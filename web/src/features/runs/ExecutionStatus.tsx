import { Activity, Check, CircleHelp, Pause, X } from "lucide-react";

export function ExecutionStatus({
  state,
  isError,
}: {
  state?: string;
  isError?: boolean;
}) {
  const normalized = (state || "recorded").toLowerCase();
  let tone: "danger" | "success" | "warning" | "neutral" = "neutral";
  if (isError || ["failed", "error"].includes(normalized)) tone = "danger";
  else if (["completed", "succeeded", "success"].includes(normalized))
    tone = "success";
  else if (
    [
      "running",
      "queued",
      "starting",
      "declined",
      "interrupted",
      "cancelled",
      "paused",
    ].includes(normalized)
  ) {
    tone = "warning";
  }
  const Icon =
    tone === "danger"
      ? X
      : tone === "success"
        ? Check
        : ["running", "queued", "starting"].includes(normalized)
          ? Activity
          : ["declined", "interrupted", "cancelled", "paused"].includes(
                normalized,
              )
            ? Pause
            : CircleHelp;
  const label = isError
    ? "Failed"
    : normalized === "recorded"
      ? "Recorded"
      : normalized.charAt(0).toUpperCase() + normalized.slice(1);
  return (
    <span className={`execution-status execution-status-${tone}`}>
      <Icon size={13} strokeWidth={2.5} aria-hidden="true" />
      {label}
    </span>
  );
}
