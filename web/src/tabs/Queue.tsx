import { useCallback, useEffect, useState } from "react";
import {
  adminDeleteQueueItem,
  adminMe,
  adminRunQueueItem,
  cdnUrl,
  getQueue,
  type QueueItem,
} from "../api";
import { formatRelative, formatTimestamp, truncate } from "../format";
import { IconPlayNow, IconRemove } from "../icons";

type State =
  | { status: "loading" }
  | { status: "ok"; items: QueueItem[] }
  | { status: "error"; message: string };

type AdminState =
  | { kind: "loading" }
  | { kind: "anon" }
  | { kind: "auth" };

const POLL_MS = 10000;

export function Queue() {
  const [state, setState] = useState<State>({ status: "loading" });
  const [admin, setAdmin] = useState<AdminState>({ kind: "loading" });
  const [refreshing, setRefreshing] = useState(false);
  const [actingId, setActingId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const tick = useCallback(
    async (signal?: AbortSignal) => {
      setRefreshing(true);
      try {
        const res = await getQueue(signal);
        setState({ status: "ok", items: res.items });
      } catch (err) {
        if (signal?.aborted) return;
        setState((prev) =>
          prev.status === "ok"
            ? prev
            : {
                status: "error",
                message: err instanceof Error ? err.message : String(err),
              },
        );
      } finally {
        setRefreshing(false);
      }
    },
    [],
  );

  useEffect(() => {
    let cancelled = false;
    let ctrl: AbortController | null = null;

    async function loop() {
      ctrl?.abort();
      ctrl = new AbortController();
      await tick(ctrl.signal);
      if (cancelled) return;
    }

    void loop();
    const id = window.setInterval(loop, POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
      ctrl?.abort();
    };
  }, [tick]);

  // Independent auth probe — the Admin tab does the same call, but we
  // need to know on this tab too so we can decide whether to render the
  // per-row action buttons.
  useEffect(() => {
    let cancelled = false;
    const ctrl = new AbortController();
    (async () => {
      try {
        const me = await adminMe(ctrl.signal);
        if (cancelled) return;
        setAdmin(me.kind === "ok" ? { kind: "auth" } : { kind: "anon" });
      } catch {
        if (cancelled || ctrl.signal.aborted) return;
        setAdmin({ kind: "anon" });
      }
    })();
    return () => {
      cancelled = true;
      ctrl.abort();
    };
  }, []);

  async function onRun(item: QueueItem) {
    setActingId(item.id);
    setActionError(null);
    try {
      await adminRunQueueItem(item.id);
      await tick();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setActingId(null);
    }
  }

  async function onRemove(item: QueueItem) {
    const ok = window.confirm(
      `Remove this queued ${item.kind} from the queue?`,
    );
    if (!ok) return;
    setActingId(item.id);
    setActionError(null);
    try {
      await adminDeleteQueueItem(item.id);
      await tick();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setActingId(null);
    }
  }

  if (state.status === "loading") {
    return (
      <>
        <SubmissionHint isAdmin={admin.kind === "auth"} />
        <QueueHeader refreshing={refreshing} />
        <p className="muted">Loading queue…</p>
      </>
    );
  }
  if (state.status === "error") {
    return (
      <>
        <SubmissionHint isAdmin={admin.kind === "auth"} />
        <QueueHeader refreshing={refreshing} />
        <div className="error">
          <p>Could not load queue.</p>
          <p className="muted small">{state.message}</p>
        </div>
      </>
    );
  }
  return (
    <>
      <SubmissionHint isAdmin={admin.kind === "auth"} />
      <QueueHeader refreshing={refreshing} />
      {actionError ? (
        <p className="admin-inline-error" role="alert">
          {actionError}
        </p>
      ) : null}
      {state.items.length === 0 ? (
        <p className="muted">Queue is empty.</p>
      ) : (
        <ol className="queue-list">
          {state.items.map((item, i) => (
            <QueueRow
              key={item.id}
              item={item}
              isHead={i === 0}
              showAdmin={admin.kind === "auth"}
              busy={actingId === item.id}
              onRun={() => onRun(item)}
              onRemove={() => onRemove(item)}
            />
          ))}
        </ol>
      )}
    </>
  );
}

function QueueHeader({ refreshing }: { refreshing: boolean }) {
  return (
    <div className="queue-status" aria-live="polite">
      <span
        className={`spinner ${refreshing ? "" : "spinner-idle"}`}
        aria-hidden="true"
      />
      <span className="muted small">
        {refreshing ? "Refreshing…" : `Auto-refresh every ${POLL_MS / 1000}s`}
      </span>
    </div>
  );
}

/**
 * Banner at the top of the Queue tab explaining how to submit. The inbound
 * email domain is build-time config (``VITE_INBOUND_EMAIL_DOMAIN``); when
 * unset, the email line is omitted and only the CLI route is shown.
 */
function SubmissionHint({ isAdmin }: { isAdmin: boolean }) {
  const emailDomain = import.meta.env.VITE_INBOUND_EMAIL_DOMAIN as
    | string
    | undefined;
  return (
    <aside className="submit-hint">
      <p className="submit-hint-heading">Add to the queue</p>
      <ul className="submit-hint-list">
        {isAdmin ? (
          <li>
            From the <strong>Admin</strong> tab — choose <em>Top</em>,{" "}
            <em>Bottom</em>, or <em>Now</em> for each prompt or image.
          </li>
        ) : null}
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

function QueueRow({
  item,
  isHead,
  showAdmin,
  busy,
  onRun,
  onRemove,
}: {
  item: QueueItem;
  isHead: boolean;
  showAdmin: boolean;
  busy: boolean;
  onRun: () => void;
  onRemove: () => void;
}) {
  return (
    <li className="queue-row">
      <div className="queue-row-meta">
        <span className="queue-id">{truncate(item.id, 10)}</span>
        <span className={`chip chip-kind chip-kind-${item.kind}`}>
          {item.kind}
        </span>
        <span className="chip chip-source">{item.source}</span>
        {isHead ? (
          <span className="chip chip-head" title="Next in line">
            up next
          </span>
        ) : null}
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
      {showAdmin ? (
        <div className="queue-row-actions">
          <button
            type="button"
            className="button enqueue-btn enqueue-btn-small enqueue-btn-now"
            title="Render this specific item now (skips queue order)"
            onClick={onRun}
            disabled={busy}
          >
            <IconPlayNow />
            <span>Run</span>
          </button>
          <button
            type="button"
            className="button enqueue-btn enqueue-btn-small enqueue-btn-danger"
            title="Remove from queue"
            onClick={onRemove}
            disabled={busy}
          >
            <IconRemove />
            <span>Remove</span>
          </button>
        </div>
      ) : null}
    </li>
  );
}
