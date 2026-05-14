import { useEffect, useState } from "react";
import { getStatus, type StatusResult } from "../api";
import {
  formatRelative,
  formatTimestamp,
  truncateHash,
} from "../format";

type State =
  | { status: "loading" }
  | { status: "ok"; result: StatusResult }
  | { status: "error"; message: string };

export function Device() {
  const [state, setState] = useState<State>({ status: "loading" });

  useEffect(() => {
    const ctrl = new AbortController();
    setState({ status: "loading" });
    getStatus(ctrl.signal)
      .then((result) => setState({ status: "ok", result }))
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
    return <p className="muted">Loading device status…</p>;
  }
  if (state.status === "error") {
    return (
      <div className="error">
        <p>Could not load device status.</p>
        <p className="muted small">{state.message}</p>
      </div>
    );
  }
  if (state.result.kind === "no_status_yet") {
    return <p className="muted">Device has not reported yet.</p>;
  }

  const s = state.result.status;
  return (
    <div className="device-card">
      <dl className="device-meta">
        <dt>Device</dt>
        <dd className="mono">{s.device_id}</dd>

        <dt>Battery</dt>
        <dd>
          {s.battery_v.toFixed(2)} V <span className="muted">·</span>{" "}
          {Math.round(s.battery_pct)}%
        </dd>

        <dt>Wi-Fi RSSI</dt>
        <dd>{s.rssi} dBm</dd>

        <dt>Last seen</dt>
        <dd title={formatTimestamp(s.last_seen)}>
          {formatRelative(s.last_seen)}{" "}
          <span className="muted small">({formatTimestamp(s.last_seen)})</span>
        </dd>

        <dt>Current hash</dt>
        <dd className="mono" title={s.current_hash}>
          {truncateHash(s.current_hash, 12, 8)}
        </dd>

        <dt>Firmware</dt>
        <dd className="mono">{s.fw_version}</dd>
      </dl>
    </div>
  );
}
