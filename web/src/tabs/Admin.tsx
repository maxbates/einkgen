import { useEffect, useState, type FormEvent } from "react";
import {
  adminEnqueueImage,
  adminEnqueuePrompt,
  adminGetFailures,
  adminGetPrompts,
  adminLogin,
  adminLogout,
  adminMe,
  adminPutPrompts,
  adminResetPrompts,
  type AdminFailureItem,
  type EnqueueAt,
} from "../api";
import { IconAddToTop, IconAddToBottom, IconPlayNow } from "../icons";

type SessionState =
  | { kind: "loading" }
  | { kind: "anon"; error?: string }
  | { kind: "auth"; exp: number }
  | { kind: "not_configured" }
  | { kind: "error"; message: string };

export function Admin() {
  const [session, setSession] = useState<SessionState>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    const ctrl = new AbortController();
    (async () => {
      try {
        const me = await adminMe(ctrl.signal);
        if (cancelled) return;
        if (me.kind === "ok") setSession({ kind: "auth", exp: me.exp });
        else if (me.kind === "not_configured") setSession({ kind: "not_configured" });
        else setSession({ kind: "anon" });
      } catch (err) {
        if (cancelled || ctrl.signal.aborted) return;
        setSession({
          kind: "error",
          message: err instanceof Error ? err.message : String(err),
        });
      }
    })();
    return () => {
      cancelled = true;
      ctrl.abort();
    };
  }, []);

  if (session.kind === "loading") {
    return <p className="muted">Checking session…</p>;
  }
  if (session.kind === "not_configured") {
    return (
      <div className="error">
        <p>Admin password isn't configured yet.</p>
        <p className="muted small">
          Run QUICKSTART §3.5 to set <code>einkgen/admin_password</code> in
          Secrets Manager, then reload.
        </p>
      </div>
    );
  }
  if (session.kind === "error") {
    return (
      <div className="error">
        <p>Could not reach admin API.</p>
        <p className="muted small">{session.message}</p>
      </div>
    );
  }
  if (session.kind === "auth") {
    return (
      <AdminPanel
        exp={session.exp}
        onLogout={() => setSession({ kind: "anon" })}
      />
    );
  }
  return (
    <LoginForm
      initialError={session.error}
      onSuccess={(exp) => setSession({ kind: "auth", exp })}
    />
  );
}

function LoginForm({
  initialError,
  onSuccess,
}: {
  initialError?: string;
  onSuccess: (exp: number) => void;
}) {
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | undefined>(initialError);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (!password) return;
    setSubmitting(true);
    setError(undefined);
    try {
      await adminLogin(password);
      // Pull fresh session to get the real exp from the server.
      const me = await adminMe();
      if (me.kind === "ok") onSuccess(me.exp);
      else setError("Login succeeded but session check failed.");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form className="admin-login" onSubmit={onSubmit}>
      <p className="submit-hint-heading">Admin sign in</p>
      <p className="muted small">
        Stays logged in for 90 days on this device.
      </p>
      <label className="admin-field">
        <span className="admin-field-label">Password</span>
        <input
          type="password"
          autoComplete="current-password"
          autoFocus
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          disabled={submitting}
          className="admin-input"
        />
      </label>
      {error ? <p className="admin-inline-error">{error}</p> : null}
      <button
        type="submit"
        className="button"
        disabled={submitting || !password}
      >
        {submitting ? "Signing in…" : "Sign in"}
      </button>
    </form>
  );
}

type EnqueueState =
  | { kind: "idle" }
  | { kind: "sending"; at: EnqueueAt }
  | { kind: "ok"; id: string; what: string; at: EnqueueAt }
  | { kind: "error"; message: string };

function AdminPanel({
  exp,
  onLogout,
}: {
  exp: number;
  onLogout: () => void;
}) {
  const [prompt, setPrompt] = useState("");
  const [imagePrompt, setImagePrompt] = useState("");
  const [imageFile, setImageFile] = useState<File | null>(null);
  const [state, setState] = useState<EnqueueState>({ kind: "idle" });

  async function submitPrompt(at: EnqueueAt) {
    if (!prompt.trim()) return;
    setState({ kind: "sending", at });
    try {
      const res = await adminEnqueuePrompt(prompt.trim(), at);
      setState({ kind: "ok", id: res.id, what: "prompt", at });
      setPrompt("");
    } catch (err) {
      setState({
        kind: "error",
        message: err instanceof Error ? err.message : String(err),
      });
    }
  }

  async function submitImage(at: EnqueueAt) {
    if (!imageFile) return;
    setState({ kind: "sending", at });
    try {
      const res = await adminEnqueueImage(
        imageFile,
        imagePrompt.trim() || null,
        at,
      );
      setState({ kind: "ok", id: res.id, what: "image", at });
      setImageFile(null);
      setImagePrompt("");
      // Reset the file input so the same file can be re-selected.
      const input = document.getElementById(
        "admin-image-input",
      ) as HTMLInputElement | null;
      if (input) input.value = "";
    } catch (err) {
      setState({
        kind: "error",
        message: err instanceof Error ? err.message : String(err),
      });
    }
  }

  async function doLogout() {
    try {
      await adminLogout();
    } finally {
      onLogout();
    }
  }

  return (
    <div className="admin-panel">
      <div className="admin-header">
        <div>
          <p className="submit-hint-heading">Admin</p>
          <p className="muted small">
            Session expires {new Date(exp * 1000).toLocaleDateString()}.
          </p>
        </div>
        <button type="button" className="button" onClick={doLogout}>
          Sign out
        </button>
      </div>

      <form
        className="admin-card"
        onSubmit={(e) => {
          // Enter in the textarea defaults to "Add to bottom" so the form
          // is keyboard-usable without picking a button.
          e.preventDefault();
          void submitPrompt("bottom");
        }}
      >
        <label className="admin-field">
          <span className="admin-field-label">Text prompt</span>
          <textarea
            className="admin-textarea"
            rows={3}
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder="A bold geometric composition…"
          />
        </label>
        <EnqueueActions
          disabled={!prompt.trim() || state.kind === "sending"}
          activeAt={state.kind === "sending" ? state.at : null}
          onClick={(at) => void submitPrompt(at)}
        />
      </form>

      <form
        className="admin-card"
        onSubmit={(e) => {
          e.preventDefault();
          void submitImage("bottom");
        }}
      >
        <label className="admin-field">
          <span className="admin-field-label">Image upload</span>
          <input
            id="admin-image-input"
            type="file"
            accept="image/*"
            onChange={(e) => setImageFile(e.target.files?.[0] ?? null)}
            className="admin-input"
          />
        </label>
        <label className="admin-field">
          <span className="admin-field-label">
            Optional restyle prompt
          </span>
          <input
            type="text"
            value={imagePrompt}
            onChange={(e) => setImagePrompt(e.target.value)}
            placeholder="(blank → publish image as B&W; with text → feed to gpt-image-2 edit)"
            className="admin-input"
          />
        </label>
        <EnqueueActions
          disabled={!imageFile || state.kind === "sending"}
          activeAt={state.kind === "sending" ? state.at : null}
          onClick={(at) => void submitImage(at)}
        />
      </form>

      {state.kind === "ok" ? (
        <p className="admin-success">
          {state.at === "now"
            ? "Queued at top and rendering now: "
            : state.at === "top"
              ? "Queued at the top of the queue: "
              : "Queued at the bottom of the queue: "}
          {state.what}{" "}
          <code className="mono">{state.id}</code>.
        </p>
      ) : state.kind === "error" ? (
        <p className="admin-inline-error">{state.message}</p>
      ) : null}

      <RecentlyRejected refreshKey={state.kind === "ok" ? state.id : null} />

      <PromptLibraryEditor />
    </div>
  );
}

function EnqueueActions({
  disabled,
  activeAt,
  onClick,
}: {
  disabled: boolean;
  activeAt: EnqueueAt | null;
  onClick: (at: EnqueueAt) => void;
}) {
  return (
    <div className="enqueue-actions">
      <button
        type="button"
        className="button enqueue-btn"
        title="Add to top of queue (next up)"
        disabled={disabled}
        onClick={() => onClick("top")}
      >
        <IconAddToTop />
        <span>{activeAt === "top" ? "Adding…" : "Top"}</span>
      </button>
      <button
        type="button"
        className="button enqueue-btn"
        title="Add to bottom of queue"
        disabled={disabled}
        onClick={() => onClick("bottom")}
      >
        <IconAddToBottom />
        <span>{activeAt === "bottom" ? "Adding…" : "Bottom"}</span>
      </button>
      <button
        type="button"
        className="button enqueue-btn enqueue-btn-now"
        title="Render this image now"
        disabled={disabled}
        onClick={() => onClick("now")}
      >
        <IconPlayNow />
        <span>{activeAt === "now" ? "Starting…" : "Now"}</span>
      </button>
    </div>
  );
}

function RecentlyRejected({ refreshKey }: { refreshKey: string | null }) {
  const [items, setItems] = useState<AdminFailureItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reloadTick, setReloadTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    const ctrl = new AbortController();
    (async () => {
      try {
        const res = await adminGetFailures(ctrl.signal);
        if (cancelled) return;
        setItems(res.items);
        setError(null);
      } catch (err) {
        if (cancelled || ctrl.signal.aborted) return;
        setError(err instanceof Error ? err.message : String(err));
      }
    })();
    return () => {
      cancelled = true;
      ctrl.abort();
    };
    // Refetch on demand (button) and whenever a new submit lands — a freshly
    // queued prompt that's about to be rejected typically shows up within a
    // minute or two, so the operator can hit refresh once they suspect.
  }, [reloadTick, refreshKey]);

  if (error) {
    return (
      <div className="admin-card">
        <p className="submit-hint-heading">Recently rejected</p>
        <p className="admin-inline-error">{error}</p>
      </div>
    );
  }

  // Hide the entire section while there's nothing to show — keeps the
  // panel clean for the happy path.
  if (items === null || items.length === 0) {
    return null;
  }

  return (
    <div className="admin-card">
      <div className="admin-failures-header">
        <div>
          <p className="submit-hint-heading">Recently rejected</p>
          <p className="muted small">
            Items the pipeline dropped in the last hour (e.g. the image model
            refused the prompt). Self-clearing — older entries disappear.
          </p>
        </div>
        <button
          type="button"
          className="button"
          onClick={() => setReloadTick((n) => n + 1)}
        >
          Refresh
        </button>
      </div>
      <ul className="admin-failure-list">
        {items.map((item) => (
          <li key={item.id} className="admin-failure-item">
            <div className="admin-failure-meta muted small">
              <span>{new Date(item.recorded_at).toLocaleString()}</span>
              <span>·</span>
              <span>via {item.source}</span>
              <span>·</span>
              <span>{item.kind}</span>
            </div>
            {item.prompt ? (
              <p className="admin-failure-prompt">{item.prompt}</p>
            ) : null}
            <p className="admin-failure-reason">{item.reason}</p>
          </li>
        ))}
      </ul>
    </div>
  );
}

type LibraryState =
  | { kind: "loading" }
  | { kind: "loaded"; text: string; persisted: string; isDefault: boolean }
  | { kind: "saving"; text: string; persisted: string; isDefault: boolean }
  | { kind: "error"; message: string };

function PromptLibraryEditor() {
  const [state, setState] = useState<LibraryState>({ kind: "loading" });
  const [notice, setNotice] = useState<
    { kind: "ok"; message: string } | { kind: "error"; message: string } | null
  >(null);

  useEffect(() => {
    let cancelled = false;
    const ctrl = new AbortController();
    (async () => {
      try {
        const res = await adminGetPrompts(ctrl.signal);
        if (cancelled) return;
        const text = res.prompts.join("\n");
        setState({
          kind: "loaded",
          text,
          persisted: text,
          isDefault: res.is_default,
        });
      } catch (err) {
        if (cancelled || ctrl.signal.aborted) return;
        setState({
          kind: "error",
          message: err instanceof Error ? err.message : String(err),
        });
      }
    })();
    return () => {
      cancelled = true;
      ctrl.abort();
    };
  }, []);

  if (state.kind === "loading") {
    return (
      <div className="admin-card">
        <p className="submit-hint-heading">Random prompt library</p>
        <p className="muted">Loading…</p>
      </div>
    );
  }
  if (state.kind === "error") {
    return (
      <div className="admin-card">
        <p className="submit-hint-heading">Random prompt library</p>
        <p className="admin-inline-error">{state.message}</p>
      </div>
    );
  }

  const isSaving = state.kind === "saving";
  const dirty = state.text !== state.persisted;
  // Same parsing the server does: trim, drop blanks + comment-only lines.
  const entryCount = state.text
    .split("\n")
    .map((line) => line.split("#")[0].trim())
    .filter((line) => line.length > 0).length;

  async function save() {
    if (state.kind !== "loaded" && state.kind !== "saving") return;
    const lines = state.text.split("\n");
    setNotice(null);
    setState({ ...state, kind: "saving" });
    try {
      const res = await adminPutPrompts(lines);
      const text = res.prompts.join("\n");
      setState({
        kind: "loaded",
        text,
        persisted: text,
        isDefault: res.is_default,
      });
      setNotice({
        kind: "ok",
        message: `Saved ${res.prompts.length} prompt${res.prompts.length === 1 ? "" : "s"}.`,
      });
    } catch (err) {
      setState({ ...state, kind: "loaded" });
      setNotice({
        kind: "error",
        message: err instanceof Error ? err.message : String(err),
      });
    }
  }

  async function reset() {
    if (state.kind !== "loaded" && state.kind !== "saving") return;
    const ok = window.confirm(
      "Replace the current library with the original 10 seed prompts? This cannot be undone.",
    );
    if (!ok) return;
    setNotice(null);
    setState({ ...state, kind: "saving" });
    try {
      const res = await adminResetPrompts();
      const text = res.prompts.join("\n");
      setState({
        kind: "loaded",
        text,
        persisted: text,
        isDefault: res.is_default,
      });
      setNotice({ kind: "ok", message: "Restored seed prompts." });
    } catch (err) {
      setState({ ...state, kind: "loaded" });
      setNotice({
        kind: "error",
        message: err instanceof Error ? err.message : String(err),
      });
    }
  }

  return (
    <div className="admin-card">
      <div>
        <p className="submit-hint-heading">Random prompt library</p>
        <p className="muted small">
          One topic per line. The cron picks from this bank when the queue
          drops below five pending items and expands each pick into a
          concrete image prompt before enqueueing. Blank lines and lines
          starting with <code>#</code> are ignored.
          {state.isDefault && !dirty ? " (Currently the seed defaults.)" : ""}
        </p>
      </div>
      <label className="admin-field">
        <span className="admin-field-label">Topics ({entryCount})</span>
        <textarea
          className="admin-textarea"
          rows={12}
          value={state.text}
          onChange={(e) =>
            setState({ ...state, kind: "loaded", text: e.target.value })
          }
          disabled={isSaving}
          spellCheck={false}
        />
      </label>
      <div className="admin-prompt-actions">
        <button
          type="button"
          className="button"
          onClick={save}
          disabled={isSaving || !dirty || entryCount === 0}
        >
          {isSaving ? "Saving…" : dirty ? "Save changes" : "Saved"}
        </button>
        <button
          type="button"
          className="button"
          onClick={reset}
          disabled={isSaving}
        >
          Reset to defaults
        </button>
      </div>
      {notice?.kind === "ok" ? (
        <p className="admin-success">{notice.message}</p>
      ) : notice?.kind === "error" ? (
        <p className="admin-inline-error">{notice.message}</p>
      ) : null}
    </div>
  );
}
