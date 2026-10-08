import { useState, type FormEvent } from "react";
import { api } from "../api";
import { useAuth } from "../auth";
import { Alert, Button, Field } from "../components/ui";
import { Icon } from "../components/icons";

export function Login() {
  const { refresh } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api("/api/admin/auth/login", { body: { email, password } });
      await refresh();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="center-page">
      <div className="auth-card">
        <div className="brand" style={{ justifyContent: "center", paddingBottom: 22 }}>
          <span className="brand-mark"><Icon name="chat" size={17} /></span>Website Assistant
        </div>
        <form className="card card-body stack" onSubmit={submit}>
          <div>
            <h1>Sign in</h1>
            <p className="muted">Use your admin account.</p>
          </div>
          {error && <Alert kind="error">{error}</Alert>}
          <Field label="Email" htmlFor="email">
            <input id="email" className="input" type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} />
          </Field>
          <Field label="Password" htmlFor="password">
            <input id="password" className="input" type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
          </Field>
          <Button kind="primary" type="submit" loading={busy}>Sign in</Button>
          <p className="small muted" style={{ margin: 0 }}>
            Forgot your password? A server administrator can run <code>docker compose exec app python cli.py reset-admin-password you@example.com</code>.
          </p>
        </form>
      </div>
    </div>
  );
}
