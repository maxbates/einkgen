import { useEffect, useState } from "react";
import { cdnUrl, getQueue, type QueueItem } from "../api";
import { formatRelative, formatTimestamp, truncate } from "../format";

type State =
  | { status: "loading" }
  | { status: "ok"; items: QueueItem[] }
  | { status: "error"; message: string };

export function Queue() {
  const [state, setState] = useState<State>({ status: "loading" });

  useEffect(() => {
    const ctrl = new AbortController();
    setState({ status: "loading" });
    getQueue(ctrl.signal)
      .then((res) => setState({ status: "ok", items: res.items }))
      .catch((err) => {
        if (ctrl.signal.aborted) return;
        setState({
          status: "error",
          message: err instanceof Error ? err.message : String(err),
        });
      });
    return () => ctrl.abort();
  }, []);

  if (state.status === "loading") {
    return <p className="muted">Loading queue…</p>;
  }
  if (state.status === "error") {
    return (
      <div className="error">
        <p>Could not load queue.</p>
        <p className="muted small">{state.message}</p>
      </div>
    );
  }
  if (state.items.length === 0) {
    return <p className="muted">Queue is empty.</p>;
  }
  return (
    <ol className="queue-list">
      {state.items.map((item) => (
        <QueueRow key={item.id} item={item} />
      ))}
    </ol>
  );
}

function QueueRow({ item }: { item: QueueItem }) {
  return (
    <li className="queue-row">
      <div className="queue-row-meta">
        <span className="queue-id">{truncate(item.id, 10)}</span>
        <span className={`chip chip-kind chip-kind-${item.kind}`}>
          {item.kind}
        </span>
        <span className="chip chip-source">{item.source}</span>
        <span
          className="queue-time muted"
          title={formatTimestamp(item.enqueued_at)}
        >
          {formatRelative(item.enqueued_at)}
        </span>
      </div>
      <div className="queue-row-body">
        {item.kind === "image" && item.image_s3_key ? (
          <img
            className="queue-thumb"
            src={cdnUrl(item.image_s3_key)}
            alt="queued upload"
            loading="lazy"
          />
        ) : null}
        {item.prompt ? (
          <p className="queue-prompt">{item.prompt}</p>
        ) : item.kind === "random" ? (
          <p className="queue-prompt muted">random subject</p>
        ) : null}
      </div>
    </li>
  );
}
