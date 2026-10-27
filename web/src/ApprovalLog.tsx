import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { api, dateTime } from "@/lib/api";

type Decision = {
  interaction_id: string;
  run_id: string;
  title?: string | null;
  kind: string;
  choice_label?: string | null;
  choice_id?: string;
  decided_by: "chatgpt" | "owner";
  reason?: string;
  decided?: string;
};

type Decider = "" | "chatgpt" | "owner";

/** The approval log: what each run asked and who answered, newest first. */
export function ApprovalLog({ refreshKey }: { refreshKey: number | null }) {
  const [decider, setDecider] = useState<Decider>("");
  const [items, setItems] = useState<Decision[] | null>(null);
  const [more, setMore] = useState<number | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let current = true;
    const query = decider ? `?decided_by=${decider}` : "";
    api<{ decisions: Decision[]; next_offset: number | null }>(
      `/api/approvals${query}`,
    )
      .then((page) => {
        if (!current) return;
        setItems(page.decisions);
        setMore(page.next_offset);
        setError("");
      })
      .catch((err: unknown) => {
        if (current) setError(err instanceof Error ? err.message : "Failed");
      });
    return () => {
      current = false;
    };
  }, [decider, refreshKey]);

  async function loadMore() {
    if (more === null) return;
    const query = decider ? `&decided_by=${decider}` : "";
    try {
      const page = await api<{
        decisions: Decision[];
        next_offset: number | null;
      }>(`/api/approvals?offset=${more}${query}`);
      setItems((previous) => [...(previous || []), ...page.decisions]);
      setMore(page.next_offset);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    }
  }

  return (
    <section className="approval-log" aria-labelledby="approval-log-title">
      <div className="section-heading">
        <div>
          <h2 id="approval-log-title">Approval log</h2>
          <p>
            Run requests that were answered. Filter by who decided to see what
            ChatGPT approved on its own.
          </p>
        </div>
        <div className="heading-action">
          <label className="sr-only" htmlFor="approval-decider">
            Decided by
          </label>
          <select
            id="approval-decider"
            className="native-select"
            value={decider}
            onChange={(event) => setDecider(event.target.value as Decider)}
          >
            <option value="">Everyone</option>
            <option value="chatgpt">ChatGPT</option>
            <option value="owner">You</option>
          </select>
        </div>
      </div>
      {error && <p role="alert">{error}</p>}
      {items && items.length === 0 && !error && (
        <p className="muted-note">No approvals have been answered yet.</p>
      )}
      {items && items.length > 0 && (
        <div className="audit-list">
          {items.map((item) => (
            <div className="audit-row" key={item.interaction_id}>
              <span className="audit-timeline" />
              <div>
                <strong>{item.title || item.kind}</strong>
                <p>
                  {item.choice_label || item.choice_id || "Answered"}, decided
                  by {item.decided_by === "owner" ? "you" : "ChatGPT"}
                  {item.reason ? `: ${item.reason}` : ""}
                </p>
              </div>
              <time dateTime={item.decided}>{dateTime(item.decided)}</time>
            </div>
          ))}
        </div>
      )}
      {more !== null && (
        <Button type="button" variant="outline" size="sm" onClick={loadMore}>
          Load more
        </Button>
      )}
    </section>
  );
}
