import { useEffect, useState, type FormEvent } from "react";
import {
  adminEnqueueImage,
  adminEnqueuePrompt,
  adminLogin,
  adminLogout,
  adminMe,
} from "../api";

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
  | { kind: "sending" }
  | { kind: "ok"; id: string; what: string }
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

  async function submitPrompt(e: FormEvent) {
    e.preventDefault();
    if (!prompt.trim()) return;
    setState({ kind: "sending" });
    try {
      const res = await adminEnqueuePrompt(prompt.trim());
      setState({ kind: "ok", id: res.id, what: "prompt" });
      setPrompt("");
    } catch (err) {
      setState({
        kind: "error",
        message: err instanceof Error ? err.message : String(err),
      });
    }
  }

  async function submitImage(e: FormEvent) {
    e.preventDefault();
    if (!imageFile) return;
    setState({ kind: "sending" });
    try {
      const res = await adminEnqueueImage(
        imageFile,
        imagePrompt.trim() || null,
      );
      setState({ kind: "ok", id: res.id, what: "image" });
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

      <form className="admin-card" onSubmit={submitPrompt}>
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
        <button
          type="submit"
          className="button"
          disabled={state.kind === "sending" || !prompt.trim()}
        >
          Enqueue prompt
        </button>
      </form>

      <form className="admin-card" onSubmit={submitImage}>
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
        <button
          type="submit"
          className="button"
          disabled={state.kind === "sending" || !imageFile}
        >
          Upload image
        </button>
      </form>

      {state.kind === "sending" ? (
        <p className="muted">Sending…</p>
      ) : state.kind === "ok" ? (
        <p className="admin-success">
          Queued {state.what}{" "}
          <code className="mono">{state.id}</code>.
        </p>
      ) : state.kind === "error" ? (
        <p className="admin-inline-error">{state.message}</p>
      ) : null}
    </div>
  );
}
