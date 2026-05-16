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
    return (
      <>
        <SubmissionHint />
        <p className="muted">Loading queue…</p>
      </>
    );
  }
  if (state.status === "error") {
    return (
      <>
        <SubmissionHint />
        <div className="error">
          <p>Could not load queue.</p>
          <p className="muted small">{state.message}</p>
        </div>
      </>
    );
  }
  return (
    <>
      <SubmissionHint />
      {state.items.length === 0 ? (
        <p className="muted">Queue is empty.</p>
      ) : (
        <ol className="queue-list">
          {state.items.map((item) => (
            <QueueRow key={item.id} item={item} />
          ))}
        </ol>
      )}
    </>
  );
}

/**
 * Banner at the top of the Queue tab explaining how to submit. The inbound
 * email domain is build-time config (``VITE_INBOUND_EMAIL_DOMAIN``); when
 * unset, the email line is omitted and only the CLI route is shown.
 */
function SubmissionHint() {
  const emailDomain = import.meta.env.VITE_INBOUND_EMAIL_DOMAIN as
    | string
    | undefined;
  return (
    <aside className="submit-hint">
      <p className="submit-hint-heading">Add to the queue</p>
      <ul className="submit-hint-list">
        <li>
          From your laptop:{" "}
          <code>einkgen queue prompt "&lt;text&gt;"</code> or{" "}
          <code>einkgen queue image &lt;path&gt;</code>
        </li>
        {emailDomain ? (
          <li>
            From anywhere: email anything <code>@{emailDomain}</code> (subject
            becomes the prompt; attach an image to upload it).
          </li>
        ) : null}
      </ul>
    </aside>
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
