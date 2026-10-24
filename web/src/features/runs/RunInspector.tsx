import { useEffect, useRef, useState, type ReactNode } from "react";
import { ArrowRight, CircleHelp, Command, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import {
  api,
  dateTime,
  displayState,
  jsonText,
  runtimeName,
  type Json,
  type Run,
} from "@/lib/api";
import type { ConfirmState, TextDetail } from "@/components/manager-types";
import type { ExecutionRecord } from "./api";
import {
  objectValue,
  executionInputPreview,
  executionOutputPreview,
} from "./feed-preview";
import { ExecutionStatus } from "./ExecutionStatus";
import { RunChanges } from "./RunChanges";
import { useRunInspector } from "./useRunInspector";

function StateBadge({ value }: { value: string }) {
  const lower = value.toLowerCase();
  const tone = /healthy|ready|completed|succeeded|enabled|active/.test(lower)
    ? "success"
    : /waiting|review|pending|paused/.test(lower)
      ? "warning"
      : /failed|error|unavailable|locked|blocked/.test(lower)
        ? "danger"
        : "neutral";
  return (
    <Badge variant="outline" className={`state-badge tone-${tone}`}>
      {value}
    </Badge>
  );
}
function Empty({
  title,
  children,
  action,
}: {
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="empty-view">
      <div className="empty-symbol">
        <CircleHelp size={22} />
      </div>
      <h3>{title}</h3>
      {children && <p>{children}</p>}
      {action}
    </div>
  );
}
function thinkingEffortLabel(effort: string) {
  return (
    (
      {
        off: "Off",
        minimal: "Minimal",
        low: "Low",
        medium: "Medium",
        high: "High",
        xhigh: "Extra high",
        max: "Maximum",
        ultra: "Ultra",
      } as Record<string, string>
    )[effort] || effort
  );
}

export function RunInspector({
  run,
  onClose,
  onNotice,
  onRefresh,
  onDetail,
  onConfirm,
}: {
  run: Run | null;
  onClose: () => void;
  onNotice: (message: string) => void;
  onRefresh: () => Promise<void>;
  onDetail: (detail: TextDetail) => void;
  onConfirm: (state: ConfirmState) => void;
}) {
  const {
    data,
    activity,
    executions,
    activityCursor,
    executionCursor,
    loadingOlder,
    load,
    loadOlder,
    updateRun,
  } = useRunInspector(run, onNotice);
  const [tab, setTab] = useState<"summary" | "activity" | "executions">(
    "summary",
  );
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [inputText, setInputText] = useState("");
  const [sendingInput, setSendingInput] = useState(false);
  const inputAttemptRef = useRef<{
    mode: "steer" | "queue";
    instruction: string;
    request_id: string;
  } | null>(null);
  const sendingInputRef = useRef(false);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const loadOlderRef = useRef<HTMLDivElement | null>(null);
  const pullStartY = useRef<number | null>(null);
  const pullDistanceRef = useRef(0);
  const pullRefreshingRef = useRef(false);
  const [pullDistance, setPullDistance] = useState(0);
  const [pullRefreshing, setPullRefreshing] = useState(false);
  useEffect(() => {
    const cursor = tab === "activity" ? activityCursor : executionCursor;
    const target = loadOlderRef.current;
    const root = panelRef.current;
    if (
      !cursor ||
      loadingOlder ||
      (tab !== "activity" && tab !== "executions") ||
      !target ||
      !root ||
      typeof IntersectionObserver === "undefined"
    ) {
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) void loadOlder(tab);
      },
      { root, rootMargin: "180px 0px" },
    );
    observer.observe(target);
    return () => observer.disconnect();
  }, [activityCursor, executionCursor, loadOlder, loadingOlder, tab]);
  useEffect(() => {
    const panel = panelRef.current;
    if (!panel || !run) return;

    const resetPull = () => {
      pullStartY.current = null;
      pullDistanceRef.current = 0;
      setPullDistance(0);
    };
    const handleTouchStart = (event: TouchEvent) => {
      if (
        event.touches.length !== 1 ||
        panel.scrollTop > 0 ||
        pullRefreshingRef.current
      ) {
        pullStartY.current = null;
        return;
      }
      pullStartY.current = event.touches[0].clientY;
    };
    const handleTouchMove = (event: TouchEvent) => {
      const startY = pullStartY.current;
      if (startY === null) return;
      if (event.touches.length !== 1 || panel.scrollTop > 0) {
        resetPull();
        return;
      }
      const distance = event.touches[0].clientY - startY;
      if (distance <= 0) {
        resetPull();
        return;
      }
      if (distance > 6) event.preventDefault();
      const nextDistance = Math.min(distance, 96);
      pullDistanceRef.current = nextDistance;
      setPullDistance(nextDistance);
    };
    const handleTouchEnd = () => {
      pullStartY.current = null;
      const shouldRefresh = pullDistanceRef.current >= 68;
      pullDistanceRef.current = 0;
      setPullDistance(0);
      if (!shouldRefresh || pullRefreshingRef.current) return;

      pullRefreshingRef.current = true;
      setPullRefreshing(true);
      void Promise.allSettled([load(run.run_id), onRefresh()])
        .then(([, pageRefresh]) => {
          if (pageRefresh.status === "rejected")
            onNotice(
              pageRefresh.reason instanceof Error
                ? pageRefresh.reason.message
                : "Unable to refresh page data.",
            );
        })
        .finally(() => {
          pullRefreshingRef.current = false;
          setPullRefreshing(false);
        });
    };

    panel.addEventListener("touchstart", handleTouchStart, { passive: true });
    panel.addEventListener("touchmove", handleTouchMove, { passive: false });
    panel.addEventListener("touchend", handleTouchEnd, { passive: true });
    panel.addEventListener("touchcancel", resetPull, { passive: true });
    return () => {
      panel.removeEventListener("touchstart", handleTouchStart);
      panel.removeEventListener("touchmove", handleTouchMove);
      panel.removeEventListener("touchend", handleTouchEnd);
      panel.removeEventListener("touchcancel", resetPull);
    };
  }, [load, onNotice, onRefresh, run]);
  async function replyInteraction(interactionId: string, body: Json) {
    if (!run) return;
    try {
      await api(
        `/api/runs/${run.run_id}/interactions/${interactionId}`,
        "POST",
        body,
      );
      onNotice("Request answered.");
      await load(run.run_id);
      await onRefresh();
    } catch (error) {
      onNotice((error as Error).message);
    }
  }
  async function sendInput(mode: "steer" | "queue") {
    if (!run || !inputText.trim() || sendingInputRef.current) return;
    const previous = inputAttemptRef.current;
    const attempt =
      previous?.mode === mode && previous.instruction === inputText
        ? previous
        : { mode, instruction: inputText, request_id: crypto.randomUUID() };
    inputAttemptRef.current = attempt;
    sendingInputRef.current = true;
    setSendingInput(true);
    try {
      const result = await api<Run>(`/api/runs/${run.run_id}/${mode}`, "POST", {
        instruction: attempt.instruction,
        request_id: attempt.request_id,
      });
      updateRun(result);
      const status = result.input_message?.status;
      if (status === "queued" || status === "delivered") {
        setInputText("");
        inputAttemptRef.current = null;
        onNotice(
          status === "queued"
            ? mode === "steer"
              ? "Correction queued."
              : "Follow-up queued."
            : "Message sent.",
        );
      } else {
        onNotice(
          status === "discarded"
            ? "Message discarded because the run stopped."
            : "Delivery is unconfirmed. This message will not be resent.",
        );
      }
    } catch (error) {
      onNotice((error as Error).message);
      // Keep the request ID: a manual retry checks the same receipt and
      // cannot submit an already accepted instruction a second time.
    } finally {
      sendingInputRef.current = false;
      setSendingInput(false);
    }
  }
  if (!run) return null;
  const current = data || run;
  const pending = (current.interactions || []).filter(
    (i) => i.state === "pending",
  );
  return (
    <Sheet open={Boolean(run)} onOpenChange={(value) => !value && onClose()}>
      <SheetContent ref={panelRef} side="right" className="run-sheet">
        <div
          className="run-pull-indicator"
          data-visible={pullRefreshing || pullDistance > 0}
          aria-hidden={!pullRefreshing && pullDistance === 0}
          aria-live="polite"
          aria-atomic="true"
        >
          <span>
            <RefreshCw size={13} className={pullRefreshing ? "spinning" : ""} />
            {pullRefreshing
              ? "Refreshing run…"
              : pullDistance >= 68
                ? "Release to refresh"
                : "Pull down to refresh"}
          </span>
        </div>
        <SheetHeader>
          <div className="inspector-state">
            <StateBadge value={displayState(current)} />{" "}
            <span>
              {current.adapter_name || "Unknown adapter"} ·{" "}
              {runtimeName(current.runtime_type || "unknown")}
            </span>
          </div>
          <SheetTitle>
            {current.handoff_title || current.job_id || "Agent run"}
          </SheetTitle>
          <SheetDescription>
            {current.workspace_name || current.workspace_id || "Workspace"} ·
            started {dateTime(current.created)}
          </SheetDescription>
        </SheetHeader>
        <div className="inspector-tabs" role="tablist" aria-label="Run detail">
          <button
            type="button"
            role="tab"
            aria-selected={tab === "summary"}
            onClick={() => setTab("summary")}
          >
            Summary
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "activity"}
            onClick={() => setTab("activity")}
          >
            Activities
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "executions"}
            onClick={() => setTab("executions")}
          >
            Executions
          </button>
        </div>
        <div className="inspector-body">
          {tab === "summary" && (
            <>
              {current.runtime_type === "opencode" &&
                current.runtime_conversation_id &&
                current.phase === "terminal" && (
                  <div className="run-result">
                    <p>
                      Open the idle session from a terminal on its Node host:
                    </p>
                    <code>
                      workspace-bridge adapter --state &lt;adapter-state&gt;
                      open-session {current.runtime_conversation_id}
                    </code>
                    <p>
                      Native handoff returns control to you. Rebind its security
                      profile while idle before resuming through the Bridge.
                    </p>
                  </div>
                )}
              <div className="run-facts">
                <div>
                  <span>Model</span>
                  <strong>
                    {current.model || "Default"}
                    {current.reasoning ? (
                      <> · {thinkingEffortLabel(current.reasoning)}</>
                    ) : null}
                  </strong>
                </div>
                <div>
                  <span>Conversation</span>
                  <strong>{current.conversation_id || "—"}</strong>
                </div>
                <div>
                  <span>Run ID</span>
                  <strong>{current.run_id}</strong>
                </div>
                <div>
                  <span>Node</span>
                  <strong>
                    {current.node_name || current.node_id || "Unknown"} · rev{" "}
                    {current.node_revision || "—"}
                  </strong>
                </div>
                <div>
                  <span>Adapter</span>
                  <strong>
                    {current.adapter_name || current.adapter_id || "Unknown"} ·
                    rev {current.adapter_revision || "—"}
                  </strong>
                </div>
              </div>
              <section className="request-panel effective-security-panel">
                <StateBadge
                  value={
                    current.effective_security?.source === "runtime-config"
                      ? "Codex runtime config"
                      : current.effective_security?.source === "profile"
                        ? "Bridge profile"
                        : "Security unavailable"
                  }
                />
                <h3>Security used for this run</h3>
                {current.effective_security?.source === "profile" ? (
                  <p>
                    Profile {current.effective_security.profile_id || "unknown"}{" "}
                    · bound revision{" "}
                    {current.effective_security.bound_revision || "unknown"} ·
                    effective revision{" "}
                    {current.effective_security.effective_revision || "unknown"}
                  </p>
                ) : current.effective_security?.source === "runtime-config" ? (
                  <>
                    <p>
                      Bound revision{" "}
                      {current.effective_security.bound_revision || "unknown"} ·
                      effective revision{" "}
                      {current.effective_security.effective_revision ||
                        "unknown"}
                    </p>
                    <p>
                      Active permission profile:{" "}
                      {String(
                        current.effective_security.resolved_summary
                          ?.activePermissionProfile || "unknown",
                      )}{" "}
                      · approval{" "}
                      {String(
                        current.effective_security.resolved_summary
                          ?.approvalPolicy || "unknown",
                      )}{" "}
                      · reviewer{" "}
                      {String(
                        current.effective_security.resolved_summary
                          ?.approvalsReviewer || "unknown",
                      )}
                    </p>
                  </>
                ) : (
                  <p>The immutable security snapshot was not available.</p>
                )}
              </section>
              <section className="request-panel token-usage-panel">
                <h3>Token usage</h3>
                {current.token_usage ? (
                  <div className="run-facts">
                    <div>
                      <span>Total</span>
                      <strong>
                        {current.token_usage.total_tokens ?? "Not reported"}
                      </strong>
                    </div>
                    <div>
                      <span>Input</span>
                      <strong>
                        {current.token_usage.input_tokens ?? "Not reported"}
                      </strong>
                    </div>
                    <div>
                      <span>Cached input</span>
                      <strong>
                        {current.token_usage.cached_input_tokens ??
                          "Not reported"}
                      </strong>
                    </div>
                    <div>
                      <span>Cache-write input</span>
                      <strong>
                        {current.token_usage.cache_write_input_tokens ??
                          "Not reported"}
                      </strong>
                    </div>
                    <div>
                      <span>Output</span>
                      <strong>
                        {current.token_usage.output_tokens ?? "Not reported"}
                      </strong>
                    </div>
                    <div>
                      <span>Reasoning output</span>
                      <strong>
                        {current.token_usage.reasoning_output_tokens ??
                          "Not reported"}
                      </strong>
                    </div>
                  </div>
                ) : (
                  <p>Not reported</p>
                )}
              </section>
              {current.checkpoint && (
                <RunChanges
                  key={current.run_id}
                  runId={current.run_id}
                  checkpoint={current.checkpoint}
                />
              )}
              {pending.map((item) => (
                <section className="request-panel" key={item.id}>
                  <StateBadge value="Needs review" />
                  <h3>{item.details?.title || item.kind}</h3>
                  <p>{item.details?.resource || ""}</p>
                  {item.details?.requested && (
                    <pre>{jsonText(item.details.requested)}</pre>
                  )}
                  {(item.details?.choices || []).map((choice) => (
                    <Button
                      key={choice.id}
                      variant={
                        choice.semantic === "approve" ? "default" : "outline"
                      }
                      size="sm"
                      onClick={() =>
                        onConfirm({
                          title: `Submit ${choice.label}?`,
                          description:
                            "This answers the live agent request and lets the same run continue.",
                          action: () =>
                            replyInteraction(item.id, { choiceId: choice.id }),
                        })
                      }
                    >
                      {choice.label}
                    </Button>
                  ))}
                  {item.kind === "form" && (
                    <>
                      <div className="form-answers">
                        {(item.details?.fields || [])
                          .filter((f) => f.id)
                          .map((field) => (
                            <div className="form-field" key={field.id}>
                              <Label htmlFor={`answer-${field.id}`}>
                                {field.question || field.header || field.id}
                              </Label>
                              <Input
                                id={`answer-${field.id}`}
                                value={answers[field.id] || ""}
                                onChange={(e) =>
                                  setAnswers((old) => ({
                                    ...old,
                                    [field.id]: e.target.value,
                                  }))
                                }
                              />
                              {field.options?.length ? (
                                <small>
                                  Options:{" "}
                                  {field.options.map((o) => o.label).join(", ")}
                                </small>
                              ) : null}
                            </div>
                          ))}
                      </div>
                      <Button
                        size="sm"
                        onClick={() => {
                          const fields = (item.details?.fields || []).filter(
                            (f) => f.id,
                          );
                          if (
                            fields.some(
                              (f) =>
                                f.required !== false && !answers[f.id]?.trim(),
                            )
                          ) {
                            onNotice(
                              "Answer every question before submitting.",
                            );
                            return;
                          }
                          void replyInteraction(item.id, {
                            answers: Object.fromEntries(
                              fields.map((f) => [
                                f.id,
                                {
                                  answers: answers[f.id]?.trim()
                                    ? [answers[f.id].trim()]
                                    : [],
                                },
                              ]),
                            ),
                          });
                        }}
                      >
                        Submit answers
                      </Button>
                    </>
                  )}
                </section>
              ))}
              {!pending.length && (
                <p className="muted-note">No live requests are waiting.</p>
              )}
              {current.phase === "active" && (
                <section className="request-panel run-input-panel">
                  <h3>Send a message</h3>
                  <p>
                    Steer corrects the current work. Queue adds a follow-up
                    after this response finishes. Claude pauses the response
                    before applying a correction.
                  </p>
                  <Label htmlFor="run-input">Correction or follow-up</Label>
                  <Textarea
                    id="run-input"
                    value={inputText}
                    maxLength={8000}
                    rows={4}
                    disabled={sendingInput}
                    onChange={(event) => {
                      setInputText(event.target.value);
                      inputAttemptRef.current = null;
                    }}
                  />
                  <div className="inspector-actions">
                    <Button
                      size="sm"
                      disabled={
                        sendingInput ||
                        !inputText.trim() ||
                        !current.input_capabilities?.steer
                      }
                      onClick={() => void sendInput("steer")}
                    >
                      Steer
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={
                        sendingInput ||
                        !inputText.trim() ||
                        !current.input_capabilities?.queue
                      }
                      onClick={() => void sendInput("queue")}
                    >
                      Queue
                    </Button>
                    <small>{inputText.length.toLocaleString()} / 8,000</small>
                  </div>
                  {!current.input_capabilities?.steer &&
                    !current.input_capabilities?.queue && (
                      <p>Message controls are unavailable for this run.</p>
                    )}
                </section>
              )}
              {Boolean(current.input_messages?.length) && (
                <section className="request-panel" aria-live="polite">
                  <h3>Recent messages</h3>
                  <ul className="run-input-receipts">
                    {current.input_messages?.slice(-5).map((message) => (
                      <li key={message.requestId}>
                        <span>
                          {message.mode === "steer"
                            ? "Correction"
                            : "Follow-up"}
                        </span>
                        <StateBadge
                          value={
                            message.status === "unknown"
                              ? "Delivery unconfirmed"
                              : message.status
                          }
                        />
                      </li>
                    ))}
                  </ul>
                  <p>
                    Pending messages are discarded if the run stops or restarts.
                  </p>
                </section>
              )}
              <div className="inspector-actions">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() =>
                    onDetail({
                      title: "Run details",
                      description:
                        "Run state, identifiers, timestamps, and recorded result metadata.",
                      content: current,
                    })
                  }
                >
                  Run details
                </Button>
                {(current.phase === "active" || current.active) && (
                  <Button
                    variant="destructive"
                    size="sm"
                    onClick={() =>
                      onConfirm({
                        title: "Stop this run?",
                        description:
                          "The active runtime conversation is cancelled. No arbitrary process is killed.",
                        destructive: true,
                        action: async () => {
                          await api(`/api/runs/${run.run_id}/stop`, "POST");
                          onNotice("Run stopped.");
                          onClose();
                        },
                      })
                    }
                  >
                    Stop run
                  </Button>
                )}
              </div>
            </>
          )}
          {tab === "activity" &&
            (activity.length ? (
              <>
                <div className="execution-list">
                  {activity.map((item) => {
                    const details = item.details;
                    const input = objectValue(details?.input);
                    const result = objectValue(details?.result);
                    const fullTitle = String(
                      details?.title ||
                        input.summary ||
                        item.kind.replace(/_/g, " "),
                    );
                    const title =
                      fullTitle.length > 120
                        ? `${fullTitle.slice(0, 117)}…`
                        : fullTitle;
                    const activityRecord: ExecutionRecord = {
                      execution_id: item.id,
                      tool: title,
                      state: item.status,
                      started: item.created,
                      target_preview:
                        typeof input.summary === "string"
                          ? input.summary
                          : undefined,
                    };
                    const failed =
                      ["failed", "error"].includes(item.status.toLowerCase()) ||
                      result.is_error === true ||
                      result.isError === true;
                    const output = executionOutputPreview(
                      details,
                      activityRecord,
                    );
                    return (
                      <article className="execution-card" key={item.id}>
                        <header className="execution-card-head">
                          <div className="execution-tool">
                            <span className="execution-tool-icon">
                              <Command size={15} aria-hidden="true" />
                            </span>
                            <div>
                              <h3 title={fullTitle}>{title}</h3>
                              <span>
                                Activity · {item.kind.replace(/_/g, " ")}
                              </span>
                            </div>
                          </div>
                          <div className="execution-meta">
                            <ExecutionStatus
                              state={item.status}
                              isError={failed}
                            />
                            {item.created ? (
                              <time dateTime={item.created}>
                                Started {dateTime(item.created)}
                              </time>
                            ) : (
                              <span>Start time unavailable</span>
                            )}
                          </div>
                        </header>
                        <div className="execution-terminal">
                          <section className="execution-preview">
                            <h4>Input</h4>
                            <pre>
                              {executionInputPreview(activityRecord, details)}
                            </pre>
                          </section>
                          <section className="execution-preview">
                            <h4>Output preview</h4>
                            <pre>{output.text}</pre>
                          </section>
                        </div>
                        <footer className="execution-card-footer">
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() =>
                              onDetail({
                                title: `Activity ${item.id}`,
                                content: details,
                              })
                            }
                          >
                            Full record <ArrowRight size={14} />
                          </Button>
                        </footer>
                      </article>
                    );
                  })}
                </div>
                {activityCursor && (
                  <div className="feed-older" ref={loadOlderRef}>
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={loadingOlder}
                      onClick={() => void loadOlder("activity")}
                    >
                      {loadingOlder
                        ? "Loading older activities…"
                        : "Load older activities"}
                    </Button>
                  </div>
                )}
              </>
            ) : (
              <Empty title="No activities recorded" />
            ))}
          {tab === "executions" &&
            (executions.length ? (
              <div className="execution-list">
                {executions.map((ex) => {
                  const recorded = activity.find(
                    (item) => item.id === ex.execution_id,
                  );
                  const details = recorded?.details;
                  const result = objectValue(details?.result);
                  const failed = ex.is_error || result.is_error === true;
                  const output = executionOutputPreview(details, ex);
                  return (
                    <article className="execution-card" key={ex.execution_id}>
                      <header className="execution-card-head">
                        <div className="execution-tool">
                          <span className="execution-tool-icon">
                            <Command size={15} aria-hidden="true" />
                          </span>
                          <div>
                            <h3>{ex.tool || "Tool"}</h3>
                            <span>Execution #{ex.sequence || "?"}</span>
                          </div>
                        </div>
                        <div className="execution-meta">
                          <ExecutionStatus state={ex.state} isError={failed} />
                          {ex.started ? (
                            <time dateTime={ex.started}>
                              Started {dateTime(ex.started)}
                            </time>
                          ) : (
                            <span>Start time unavailable</span>
                          )}
                        </div>
                      </header>
                      <div className="execution-terminal">
                        <section className="execution-preview">
                          <h4>Input</h4>
                          <pre>{executionInputPreview(ex, details)}</pre>
                        </section>
                        <section className="execution-preview">
                          <h4>Output preview</h4>
                          <pre>{output.text}</pre>
                        </section>
                      </div>
                      <footer className="execution-card-footer">
                        {typeof ex.duration_ms === "number" && (
                          <span>{(ex.duration_ms / 1000).toFixed(1)}s</span>
                        )}
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={async () => {
                            try {
                              const detail = await api(
                                `/api/runs/${run.run_id}/executions/${encodeURIComponent(ex.execution_id)}`,
                              );
                              onDetail({
                                title: `Execution ${ex.execution_id}`,
                                content: detail,
                              });
                            } catch (error) {
                              onNotice((error as Error).message);
                            }
                          }}
                        >
                          Full record <ArrowRight size={14} />
                        </Button>
                      </footer>
                    </article>
                  );
                })}
                {executionCursor && (
                  <div className="feed-older" ref={loadOlderRef}>
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={loadingOlder}
                      onClick={() => void loadOlder("executions")}
                    >
                      {loadingOlder
                        ? "Loading older executions…"
                        : "Load older executions"}
                    </Button>
                  </div>
                )}
              </div>
            ) : (
              <Empty title="No executions recorded" />
            ))}
        </div>
      </SheetContent>
    </Sheet>
  );
}
