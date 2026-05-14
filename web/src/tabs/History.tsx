import { useEffect, useState } from "react";
import { cdnUrl, getHistory, type HistoryItem } from "../api";
import {
  formatRelative,
  formatTimestamp,
  truncateHash,
} from "../format";

const PAGE_SIZE = 50;

type State =
  | { status: "loading" }
  | { status: "ok"; items: HistoryItem[]; limit: number; loadingMore: boolean }
  | { status: "error"; message: string };

export function History() {
  const [state, setState] = useState<State>({ status: "loading" });
  const [selected, setSelected] = useState<HistoryItem | null>(null);

  useEffect(() => {
    const ctrl = new AbortController();
    setState({ status: "loading" });
    getHistory(PAGE_SIZE, ctrl.signal)
      .then((res) =>
        setState({
          status: "ok",
          items: res.items,
          limit: PAGE_SIZE,
          loadingMore: false,
        }),
      )
      .catch((err) => {
        if (ctrl.signal.aborted) return;
        setState({
          status: "error",
          message: err instanceof Error ? err.message : String(err),
        });
      });
    return () => ctrl.abort();
  }, []);

  function loadMore() {
    if (state.status !== "ok" || state.loadingMore) return;
    const nextLimit = state.limit + PAGE_SIZE;
    setState({ ...state, loadingMore: true });
    getHistory(nextLimit)
      .then((res) =>
        setState({
          status: "ok",
          items: res.items,
          limit: nextLimit,
          loadingMore: false,
        }),
      )
      .catch((err) =>
        setState({
          status: "error",
          message: err instanceof Error ? err.message : String(err),
        }),
      );
  }

  if (state.status === "loading") {
    return <p className="muted">Loading history…</p>;
  }
  if (state.status === "error") {
    return (
      <div className="error">
        <p>Could not load history.</p>
        <p className="muted small">{state.message}</p>
      </div>
    );
  }
  if (state.items.length === 0) {
    return <p className="muted">No frames published yet.</p>;
  }
  const fullyLoaded = state.items.length < state.limit;
  return (
    <>
      <div className="history-grid">
        {state.items.map((item) => (
          <HistoryTile
            key={item.id}
            item={item}
            onClick={() => setSelected(item)}
          />
        ))}
      </div>
      <div className="history-footer">
        {fullyLoaded ? (
          <p className="muted small">All {state.items.length} frames loaded.</p>
        ) : (
          <button
            className="button"
            onClick={loadMore}
            disabled={state.loadingMore}
          >
            {state.loadingMore ? "Loading…" : "Load more"}
          </button>
        )}
      </div>
      {selected ? (
        <HistoryDetails
          item={selected}
          onClose={() => setSelected(null)}
        />
      ) : null}
    </>
  );
}

function HistoryTile({
  item,
  onClick,
}: {
  item: HistoryItem;
  onClick: () => void;
}) {
  const sourceLine =
    item.source.prompt ?? (item.source.kind === "image" ? "uploaded" : item.source.kind);
  return (
    <button className="history-tile" onClick={onClick}>
      <img
        className="history-img"
        src={cdnUrl(`history/${item.id}/processed.bmp`)}
        alt={sourceLine}
        loading="lazy"
      />
      <div className="history-meta">
        <p className="history-source">{sourceLine}</p>
        <p
          className="history-time muted small"
          title={formatTimestamp(item.generated_at)}
        >
          {formatRelative(item.generated_at)}
        </p>
      </div>
    </button>
  );
}

function HistoryDetails({
  item,
  onClose,
}: {
  item: HistoryItem;
  onClose: () => void;
}) {
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const sourceLine =
    item.source.prompt ?? (item.source.kind === "image" ? "uploaded" : item.source.kind);
  return (
    <div
      className="modal-backdrop"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
    >
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <button className="modal-close" onClick={onClose} aria-label="Close">
          Close
        </button>
        <img
          className="modal-img"
          src={cdnUrl(`history/${item.id}/processed.bmp`)}
          alt={sourceLine}
        />
        <dl className="modal-meta">
          <dt>Source</dt>
          <dd>{sourceLine}</dd>
          {item.source.model ? (
            <>
              <dt>Model</dt>
              <dd>{item.source.model}</dd>
            </>
          ) : null}
          <dt>Generated</dt>
          <dd title={formatTimestamp(item.generated_at)}>
            {formatTimestamp(item.generated_at)} ({formatRelative(item.generated_at)})
          </dd>
          <dt>Hash</dt>
          <dd className="mono" title={item.image_sha256}>
            {truncateHash(item.image_sha256, 12, 8)}
          </dd>
          <dt>ID</dt>
          <dd className="mono">{item.id}</dd>
        </dl>
      </div>
    </div>
  );
}
