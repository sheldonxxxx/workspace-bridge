import { useCallback, useEffect, useRef, useState } from "react";
import type { Run } from "@/lib/api";
import {
  getOlderActivities,
  getOlderExecutions,
  getRunSnapshot,
  type ExecutionRecord,
  type FeedCursor,
  type RunActivity,
} from "./api";
import { appendOlder, mergeNewest, newestPageHasGap } from "./feed-state";
import { createRefreshQueue } from "./refresh-queue";
import { useManagerEvents } from "./useManagerEvents";

type Feed = "activity" | "executions";
type Feeds = {
  runId?: string;
  activity: RunActivity[];
  executions: ExecutionRecord[];
  activityCursor: FeedCursor | null;
  executionCursor: FeedCursor | null;
};
type Session = {
  id: string;
  controller: AbortController;
  queue: ReturnType<typeof createRefreshQueue>;
  olderBusy: boolean;
  olderLoaded: Record<Feed, boolean>;
  feeds: Feeds;
  revisions: Record<Feed, number>;
};
const emptyFeeds = (runId?: string): Feeds => ({
  runId,
  activity: [],
  executions: [],
  activityCursor: null,
  executionCursor: null,
});

export function useRunInspector(
  run: Run | null,
  onNotice: (message: string) => void,
) {
  const runId = run?.run_id;
  const [data, setData] = useState(run);
  const [feeds, setFeeds] = useState<Feeds>(() => emptyFeeds(runId));
  const [loadingOlderRun, setLoadingOlderRun] = useState<string | null>(null);
  const sessionRef = useRef<Session | null>(null);
  const onNoticeRef = useRef(onNotice);
  useEffect(() => {
    onNoticeRef.current = onNotice;
  }, [onNotice]);
  const current = useCallback(
    (session: Session) =>
      sessionRef.current === session && !session.controller.signal.aborted,
    [],
  );

  useEffect(() => {
    if (!runId) return;
    const controller = new AbortController();
    const session: Session = {
      id: runId,
      controller,
      queue: createRefreshQueue(async () => {
        try {
          const snapshot = await getRunSnapshot(runId, controller.signal);
          if (!current(session)) return;
          setData(snapshot.run);
          if (!session.olderBusy) setLoadingOlderRun(null);
          const previous = session.feeds;
          const activityGap =
            snapshot.activity &&
            newestPageHasGap(
              snapshot.activity.activities || [],
              previous.activity,
              (item) => item.id,
              Boolean(snapshot.activity.next_cursor),
            );
          const executionGap =
            snapshot.executions &&
            newestPageHasGap(
              snapshot.executions.executions || [],
              previous.executions,
              (item) => item.execution_id,
              Boolean(snapshot.executions.next_cursor),
            );
          if (activityGap) {
            session.revisions.activity++;
            session.olderLoaded.activity = false;
          }
          if (executionGap) {
            session.revisions.executions++;
            session.olderLoaded.executions = false;
          }
          session.feeds = {
            ...previous,
            activity: snapshot.activity
              ? activityGap
                ? snapshot.activity.activities || []
                : mergeNewest(
                    snapshot.activity.activities || [],
                    previous.activity,
                    (item) => item.id,
                  )
              : previous.activity,
            executions: snapshot.executions
              ? executionGap
                ? snapshot.executions.executions || []
                : mergeNewest(
                    snapshot.executions.executions || [],
                    previous.executions,
                    (item) => item.execution_id,
                  )
              : previous.executions,
            activityCursor:
              snapshot.activity && !session.olderLoaded.activity
                ? snapshot.activity.next_cursor || null
                : previous.activityCursor,
            executionCursor:
              snapshot.executions && !session.olderLoaded.executions
                ? snapshot.executions.next_cursor || null
                : previous.executionCursor,
          };
          setFeeds(session.feeds);
        } catch (error) {
          if (current(session))
            onNoticeRef.current(
              error instanceof Error ? error.message : "Unable to refresh run.",
            );
        }
      }),
      olderBusy: false,
      olderLoaded: { activity: false, executions: false },
      feeds: emptyFeeds(runId),
      revisions: { activity: 0, executions: 0 },
    };
    sessionRef.current = session;
    void session.queue.refresh();
    const reconcile = () => {
      if (!document.hidden) session.queue.schedule();
    };
    const timer = window.setInterval(reconcile, 30_000);
    document.addEventListener("visibilitychange", reconcile);
    return () => {
      session.queue.dispose();
      controller.abort();
      if (sessionRef.current === session) sessionRef.current = null;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", reconcile);
    };
  }, [runId, current]);

  const load = useCallback(async (id?: string) => {
    const session = sessionRef.current;
    if (session && (!id || id === session.id)) await session.queue.refresh();
  }, []);
  const changed = useCallback(() => {
    if (!document.hidden) sessionRef.current?.queue.schedule();
  }, []);
  useManagerEvents({
    enabled: Boolean(runId),
    runId,
    onRun: changed,
    onReset: changed,
  });

  const loadOlder = useCallback(
    async (feed: Feed) => {
      const session = sessionRef.current;
      const cursor =
        feeds.runId !== runId
          ? null
          : feed === "activity"
            ? feeds.activityCursor
            : feeds.executionCursor;
      if (!session || session.id !== runId || !cursor || session.olderBusy)
        return;
      session.olderBusy = true;
      const wasLoaded = session.olderLoaded[feed];
      const revision = session.revisions[feed];
      // A concurrent newest-page read must not move a cursor being consumed.
      session.olderLoaded[feed] = true;
      setLoadingOlderRun(session.id);
      try {
        if (feed === "activity") {
          const page = await getOlderActivities(
            session.id,
            cursor,
            session.controller.signal,
          );
          if (!current(session) || revision !== session.revisions[feed]) return;
          session.feeds = {
            ...session.feeds,
            activity: appendOlder(
              session.feeds.activity,
              page.activities || [],
              (item) => item.id,
            ),
            activityCursor: page.next_cursor || null,
          };
          setFeeds(session.feeds);
        } else {
          const page = await getOlderExecutions(
            session.id,
            cursor,
            session.controller.signal,
          );
          if (!current(session) || revision !== session.revisions[feed]) return;
          session.feeds = {
            ...session.feeds,
            executions: appendOlder(
              session.feeds.executions,
              page.executions || [],
              (item) => item.execution_id,
            ),
            executionCursor: page.next_cursor || null,
          };
          setFeeds(session.feeds);
        }
      } catch (error) {
        if (current(session) && revision === session.revisions[feed]) {
          session.olderLoaded[feed] = wasLoaded;
          onNoticeRef.current(
            error instanceof Error
              ? error.message
              : "Unable to load older records.",
          );
        }
      } finally {
        session.olderBusy = false;
        if (current(session)) setLoadingOlderRun(null);
      }
    },
    [feeds.runId, feeds.activityCursor, feeds.executionCursor, runId, current],
  );

  const visibleFeeds = feeds.runId === runId ? feeds : emptyFeeds(runId);
  return {
    data: data?.run_id === runId ? data : run,
    ...visibleFeeds,
    loadingOlder: loadingOlderRun === runId,
    load,
    loadOlder,
  };
}
