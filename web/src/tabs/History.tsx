import { useEffect, useState } from "react";
import {
  adminMe,
  adminShowHistory,
  cdnUrl,
  getCurrentManifest,
  getHistory,
  type CurrentManifest,
  type HistoryItem,
} from "../api";
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
  const [current, setCurrent] = useState<CurrentManifest | null>(null);
  const [authed, setAuthed] = useState(false);

  useEffect(() => {
    const ctrl = new AbortController();
    setState({ status: "loading" });
    Promise.all([
      getHistory(PAGE_SIZE, ctrl.signal),
      getCurrentManifest(ctrl.signal).catch(() => null),
      adminMe(ctrl.signal).catch(() => ({ kind: "unauthenticated" as const })),
    ])
      .then(([hist, cur, me]) => {
        setState({
          status: "ok",
          items: hist.items,
          limit: PAGE_SIZE,
          loadingMore: false,
        });
        setCurrent(cur);
        setAuthed(me.kind === "ok");
      })
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

  async function refreshCurrent() {
    try {
      const cur = await getCurrentManifest();
      setCurrent(cur);
    } catch {
      // Non-fatal — the indicator just won't update this tick.
    }
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
  const currentId = currentlyShowingId(current, state.items);
  return (
    <>
      <div className="history-grid">
        {state.items.map((item) => (
          <HistoryTile
            key={item.id}
            item={item}
            isCurrent={item.id === currentId}
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
          isCurrent={selected.id === currentId}
          authed={authed}
          onShown={refreshCurrent}
          onClose={() => setSelected(null)}
        />
      ) : null}
    </>
  );
}

/**
 * Match the current manifest back to a history tile. Prefer the explicit
 * ``source.replayed_from`` marker (set by /admin/show) so we never get fooled
 * by two history items sharing a sha256; fall back to the sha256 match for
 * everything else (cron, queue prompt, admin queue, email).
 */
function currentlyShowingId(
  current: CurrentManifest | null,
  items: HistoryItem[],
): string | null {
  if (!current) return null;
  const replayedFrom = current.source.replayed_from;
  if (replayedFrom) return replayedFrom;
  const match = items.find((i) => i.image_sha256 === current.image_sha256);
  return match ? match.id : null;
}

function HistoryTile({
  item,
  isCurrent,
  onClick,
}: {
  item: HistoryItem;
  isCurrent: boolean;
  onClick: () => void;
}) {
  const sourceLine = item.source.prompt ?? item.source.kind;
  return (
    <button
      className={`history-tile ${isCurrent ? "history-tile-current" : ""}`}
      onClick={onClick}
    >
      <div className="history-img-wrap">
        <img
          className="history-img"
          src={cdnUrl(`history/${item.id}/processed.bmp`)}
          alt={sourceLine}
          loading="lazy"
        />
        {isCurrent ? (
          <span
            className="history-now-badge"
            title="Currently shown on the device"
            aria-label="Currently shown on the device"
          >
            <EyeIcon />
            <span>Now showing</span>
          </span>
        ) : null}
      </div>
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

type ShowState =
  | { kind: "idle" }
  | { kind: "sending" }
  | { kind: "ok" }
  | { kind: "error"; message: string };

function HistoryDetails({
  item,
  isCurrent,
  authed,
  onShown,
  onClose,
}: {
  item: HistoryItem;
  isCurrent: boolean;
  authed: boolean;
  onShown: () => void;
  onClose: () => void;
}) {
  const [showState, setShowState] = useState<ShowState>({ kind: "idle" });

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  async function pushToDevice() {
    setShowState({ kind: "sending" });
    try {
      await adminShowHistory(item.id);
      setShowState({ kind: "ok" });
      onShown();
    } catch (err) {
      setShowState({
        kind: "error",
        message: err instanceof Error ? err.message : String(err),
      });
    }
  }

  const sourceLine = item.source.prompt ?? item.source.kind;
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
        {authed ? (
          <div className="modal-actions">
            {isCurrent ? (
              <p className="muted small">
                <EyeIcon /> This is what the device is currently showing.
              </p>
            ) : (
              <button
                type="button"
                className="button"
                onClick={pushToDevice}
                disabled={showState.kind === "sending"}
              >
                {showState.kind === "sending" ? "Sending…" : "Show this now"}
              </button>
            )}
            {showState.kind === "ok" && !isCurrent ? (
              <p className="admin-success">
                Set as current. The device will pick it up on its next wake.
              </p>
            ) : null}
            {showState.kind === "error" ? (
              <p className="admin-inline-error">{showState.message}</p>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}

function EyeIcon() {
  return (
    <svg
      className="eye-icon"
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  );
}
