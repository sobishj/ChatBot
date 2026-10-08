// First-run setup wizard: admin → mode → domain → AI model → embeddings → (client) → done.
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api, type AIModel, type Client, type Job, type Provider, type User } from "../api";
import { useAuth } from "../auth";
import { JobProgress } from "../components/JobProgress";
import { emptyModel, ModelForm, modelToValues, payload, type ModelValues, type TestResult } from "../components/ModelForm";
import { Alert, Button, Field, Loading } from "../components/ui";
import { Icon } from "../components/icons";

interface SetupState {
  completed: boolean;
  step: number;
  has_admin: boolean;
  user: User | null;
  mode: "cloud" | "onprem";
  public_domain: string;
  model: AIModel | null;
  embedding: { model: string; dim: number | null; status: string; error: string | null };
  embedding_job: Job | null;
  client: Client | null;
  providers: Provider[];
}

const STEP_NAMES = ["Admin account", "Mode", "Public domain", "AI model", "Embeddings", "Client", "Done"];

export function Setup() {
  const { refresh } = useAuth();
  const navigate = useNavigate();
  const [state, setState] = useState<SetupState | null>(null);
  const [step, setStep] = useState(1);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    const s = await api<SetupState>("/api/admin/setup/state");
    setState(s);
    setStep(s.has_admin && !s.user ? 0 : s.step);
    return s;
  }, []);

  useEffect(() => {
    load().then((s) => s.completed && navigate("/", { replace: true }));
  }, [load, navigate]);

  const call = async (path: string, body?: unknown) => {
    setBusy(true);
    setError("");
    try {
      const res = await api(path, { method: "POST", body: body ?? {} });
      await load();
      return res;
    } catch (e) {
      setError((e as Error).message);
      return null;
    } finally {
      setBusy(false);
    }
  };

  if (!state) return <div className="center-page"><Loading /></div>;
  const onprem = state.mode === "onprem";
  const visibleSteps = STEP_NAMES.map((name, i) => ({ name, n: i + 1 })).filter((s) => s.n !== 6 || onprem);

  return (
    <div className="center-page">
      <div className="wizard">
        <div className="brand" style={{ paddingBottom: 14 }}>
          <span className="brand-mark"><Icon name="chat" size={17} /></span>Website Assistant setup
        </div>
        {step > 0 && (
          <div className="steps" aria-label="Setup progress">
            {visibleSteps.map((s, i) => (
              <span key={s.n} className={`step-pill ${s.n === step ? "current" : s.n < step ? "done" : ""}`}>
                <span className="n">{s.n < step ? "✓" : i + 1}</span>{s.name}
              </span>
            ))}
          </div>
        )}
        <div className="card card-body stack">
          {error && <Alert kind="error">{error}</Alert>}
          {step === 0 && <SignInFirst onDone={async () => { await refresh(); await load(); }} />}
          {step === 1 && <AdminStep busy={busy} onSubmit={async (b) => { if (await call("/api/admin/setup/admin", b)) await refresh(); }} />}
          {step === 2 && <ModeStep value={state.mode} busy={busy} onSubmit={(mode) => call("/api/admin/setup/mode", { mode })} />}
          {step === 3 && <DomainStep value={state.public_domain} busy={busy} onBack={() => setStep(2)} onSubmit={(d) => call("/api/admin/setup/domain", { public_domain: d })} />}
          {step === 4 && <ModelStep state={state} onBack={() => setStep(3)} onSaved={load} />}
          {step === 5 && <EmbeddingStep state={state} busy={busy} onBack={() => setStep(4)} onStart={(model) => call("/api/admin/setup/embedding", { model })} onContinue={() => call("/api/admin/setup/embedding/continue")} reload={load} />}
          {step === 6 && <ClientStep busy={busy} onBack={() => setStep(5)} onSubmit={(b) => call("/api/admin/setup/client", b)} />}
          {step === 7 && <DoneStep state={state} busy={busy} onFinish={async () => { if (await call("/api/admin/setup/finish")) { await refresh(); navigate("/", { replace: true }); } }} />}
        </div>
      </div>
    </div>
  );
}

function SignInFirst({ onDone }: { onDone: () => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    try {
      await api("/api/admin/auth/login", { body: { email, password } });
      onDone();
    } catch (err) {
      setError((err as Error).message);
    }
  };
  return (
    <form className="stack" onSubmit={submit}>
      <h1>Continue setup</h1>
      <p className="muted">The admin account already exists. Sign in to finish setting up.</p>
      {error && <Alert kind="error">{error}</Alert>}
      <Field label="Email" htmlFor="s-email"><input id="s-email" className="input" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required /></Field>
      <Field label="Password" htmlFor="s-pass"><input id="s-pass" className="input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} required /></Field>
      <div className="row end"><Button kind="primary" type="submit">Sign in</Button></div>
    </form>
  );
}

function AdminStep({ busy, onSubmit }: { busy: boolean; onSubmit: (b: { name: string; email: string; password: string }) => void }) {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const mismatch = confirm.length > 0 && confirm !== password;
  return (
    <form className="stack" onSubmit={(e) => { e.preventDefault(); if (!mismatch) onSubmit({ name, email, password }); }}>
      <div><h1>Create the super admin</h1><p className="muted">This account can manage everything. You can add more users later.</p></div>
      <div className="form-grid">
        <Field label="Full name" htmlFor="a-name" full><input id="a-name" className="input" required value={name} onChange={(e) => setName(e.target.value)} /></Field>
        <Field label="Email" htmlFor="a-email" full><input id="a-email" className="input" type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} /></Field>
        <Field label="Password" htmlFor="a-pass" hint="At least 10 characters."><input id="a-pass" className="input" type="password" autoComplete="new-password" minLength={10} required value={password} onChange={(e) => setPassword(e.target.value)} /></Field>
        <Field label="Confirm password" htmlFor="a-pass2" hint={mismatch ? "Passwords don't match." : undefined}><input id="a-pass2" className="input" type="password" autoComplete="new-password" required value={confirm} onChange={(e) => setConfirm(e.target.value)} /></Field>
      </div>
      <div className="row end"><Button kind="primary" type="submit" loading={busy} disabled={mismatch}>Create account</Button></div>
    </form>
  );
}

function ModeStep({ value, busy, onSubmit }: { value: string; busy: boolean; onSubmit: (m: string) => void }) {
  const [mode, setMode] = useState(value || "cloud");
  return (
    <div className="stack">
      <div><h1>How will this server be used?</h1><p className="muted">This only changes the admin UI and warnings; you can't switch later without reinstalling.</p></div>
      <div className="grid grid-2">
        <button type="button" className={`choice ${mode === "cloud" ? "selected" : ""}`} onClick={() => setMode("cloud")} aria-pressed={mode === "cloud"}>
          <h3>Cloud (multi-client)</h3>
          <p className="muted small">Host assistants for many businesses on this server. Each client has its own content, branding and stats.</p>
        </button>
        <button type="button" className={`choice ${mode === "onprem" ? "selected" : ""}`} onClick={() => setMode("onprem")} aria-pressed={mode === "onprem"}>
          <h3>On-premise (single client)</h3>
          <p className="muted small">Runs on the customer's own server, usually with a local AI model, so data stays inside.</p>
        </button>
      </div>
      <div className="row end"><Button kind="primary" loading={busy} onClick={() => onSubmit(mode)}>Continue</Button></div>
    </div>
  );
}

function DomainStep({ value, busy, onBack, onSubmit }: { value: string; busy: boolean; onBack: () => void; onSubmit: (d: string) => void }) {
  const [domain, setDomain] = useState(value);
  return (
    <form className="stack" onSubmit={(e) => { e.preventDefault(); onSubmit(domain); }}>
      <div><h1>Public domain</h1><p className="muted">The address websites use to load the chat widget, e.g. <code>chat.example.com</code>. It goes into every embed code.</p></div>
      <Field label="Domain" htmlFor="d-domain" hint="No http:// and no path. For local testing you can use localhost:8000.">
        <input id="d-domain" className="input" required value={domain} onChange={(e) => setDomain(e.target.value)} placeholder="chat.example.com" />
      </Field>
      <div className="row between"><Button onClick={onBack}>Back</Button><Button kind="primary" type="submit" loading={busy}>Continue</Button></div>
    </form>
  );
}

function ModelStep({ state, onBack, onSaved }: { state: SetupState; onBack: () => void; onSaved: () => Promise<unknown> }) {
  const local = state.providers.find((p) => p.key === (state.mode === "onprem" ? "bionic" : "openai"));
  const [values, setValues] = useState<ModelValues>(state.model ? modelToValues(state.model) : { ...emptyModel(local), name: local?.label.split(" (")[0] ?? "" });
  const [test, setTest] = useState<TestResult | null>(null);
  const [testing, setTesting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const runTest = async () => {
    setTesting(true);
    setTest(null);
    try {
      setTest(await api<TestResult>("/api/admin/setup/model/test", { body: payload(values) }));
    } catch (e) {
      setTest({ ok: false, error: (e as Error).message });
    } finally {
      setTesting(false);
    }
  };
  const save = async () => {
    setSaving(true);
    setError("");
    try {
      await api("/api/admin/setup/model", { body: payload(values) });
      await onSaved();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="stack">
      <div><h1>Add your first AI model</h1><p className="muted">Any provider works: a hosted API or a local server like Bionic, Ollama or vLLM. The connection test must succeed before you continue. You can add more models later.</p></div>
      {error && <Alert kind="error">{error}</Alert>}
      <ModelForm values={values} onChange={(v) => { setValues(v); setTest(null); }} providers={state.providers} mode={state.mode} editing={!!state.model} hasStoredKey={state.model?.has_api_key} onTest={runTest} testResult={test} testing={testing} />
      <div className="row between">
        <Button onClick={onBack}>Back</Button>
        <Button kind="primary" onClick={save} loading={saving} disabled={!test?.ok}>Save and continue</Button>
      </div>
    </div>
  );
}

function EmbeddingStep({ state, busy, onBack, onStart, onContinue, reload }: { state: SetupState; busy: boolean; onBack: () => void; onStart: (m: string) => void; onContinue: () => void; reload: () => Promise<unknown> }) {
  const [model, setModel] = useState(state.embedding.model || "BAAI/bge-m3");
  const job = state.embedding_job;
  const status = state.embedding.status;
  return (
    <div className="stack">
      <div>
        <h1>Embedding model</h1>
        <p className="muted">Turns your content into vectors for search. <strong>BAAI/bge-m3</strong> is multilingual (English, Malayalam, Hindi, Arabic and 100+ more) and runs on the CPU. The download is about 2.3 GB and happens once.</p>
      </div>
      <Field label="Hugging Face model" htmlFor="e-model" hint="Changing this later re-indexes all content.">
        <input id="e-model" className="input" value={model} onChange={(e) => setModel(e.target.value)} disabled={status === "downloading"} />
      </Field>
      {state.embedding.error && <Alert kind="error">Download failed: {state.embedding.error}</Alert>}
      {job && status === "downloading" && <JobProgress jobId={job.id} onDone={() => reload()} />}
      {status === "ready" && <Alert kind="success">{state.embedding.model} is ready (vector size {state.embedding.dim}).</Alert>}
      {status === "downloading" && <p className="small muted">You can continue while it downloads; crawling and chat start working once it's ready.</p>}
      <div className="row between">
        <Button onClick={onBack}>Back</Button>
        <div className="row">
          {status !== "downloading" && status !== "ready" && <Button kind="primary" icon="download" loading={busy} onClick={() => onStart(model)}>Download</Button>}
          {(status === "downloading" || status === "ready") && <Button kind="primary" loading={busy} onClick={onContinue}>Continue</Button>}
        </div>
      </div>
    </div>
  );
}

function ClientStep({ busy, onBack, onSubmit }: { busy: boolean; onBack: () => void; onSubmit: (b: object) => void }) {
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [website, setWebsite] = useState("");
  const [domains, setDomains] = useState("");
  const autoSlug = (n: string) => n.toLowerCase().normalize("NFKD").replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 64);
  return (
    <form className="stack" onSubmit={(e) => { e.preventDefault(); onSubmit({ client_id: slug, name, website_url: website, allowed_domains: domains.split(/[\s,]+/).filter(Boolean) }); }}>
      <div><h1>Create the client</h1><p className="muted">The organisation this on-premise assistant serves.</p></div>
      <div className="form-grid">
        <Field label="Organisation name" htmlFor="c-name"><input id="c-name" className="input" required value={name} onChange={(e) => { setName(e.target.value); if (!slug || slug === autoSlug(name)) setSlug(autoSlug(e.target.value)); }} /></Field>
        <Field label="Client ID" htmlFor="c-slug" hint="Lowercase letters, digits and hyphens. Used in the embed code."><input id="c-slug" className="input" required value={slug} onChange={(e) => setSlug(e.target.value)} /></Field>
        <Field label="Website URL" htmlFor="c-web" full><input id="c-web" className="input" value={website} onChange={(e) => setWebsite(e.target.value)} placeholder="https://www.example.com" /></Field>
        <Field label="Allowed domains" htmlFor="c-dom" full hint="Websites allowed to show the widget, separated by commas. Defaults to the website's domain."><input id="c-dom" className="input" value={domains} onChange={(e) => setDomains(e.target.value)} placeholder="example.com, intranet.example.com" /></Field>
      </div>
      <div className="row between"><Button onClick={onBack}>Back</Button><Button kind="primary" type="submit" loading={busy}>Create client</Button></div>
    </form>
  );
}

function DoneStep({ state, busy, onFinish }: { state: SetupState; busy: boolean; onFinish: () => void }) {
  return (
    <div className="stack">
      <div><h1>All set</h1><p className="muted">Review and open the dashboard.</p></div>
      <dl className="kv">
        <dt>Mode</dt><dd>{state.mode === "onprem" ? "On-premise (single client)" : "Cloud (multi-client)"}</dd>
        <dt>Public domain</dt><dd>{state.public_domain}</dd>
        <dt>AI model</dt><dd>{state.model?.name} · {state.model?.model_name}</dd>
        <dt>Embeddings</dt><dd>{state.embedding.model} · {state.embedding.status}</dd>
        {state.client && (<><dt>Client</dt><dd>{state.client.name} ({state.client.client_id})</dd></>)}
      </dl>
      <Alert kind="info">Next: {state.mode === "onprem" ? "open the assistant" : "add a client"}, run <strong>Crawl now</strong>, then try it in <strong>Test chat</strong>.</Alert>
      <div className="row end"><Button kind="primary" loading={busy} onClick={onFinish}>Open dashboard</Button></div>
    </div>
  );
}
