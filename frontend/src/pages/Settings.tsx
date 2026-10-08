// Settings (super admin): AI models + system settings.
import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, type AIModel, type Job, type Provider } from "../api";
import { JobProgress } from "../components/JobProgress";
import { PageHead } from "../components/Layout";
import { emptyModel, ModelForm, modelToValues, payload, type ModelValues, type TestResult } from "../components/ModelForm";
import { Alert, Badge, Button, Card, Empty, Field, Loading, Modal, Tabs, useAction } from "../components/ui";
import { relative } from "../format";

type Tab = "models" | "answers" | "crawling" | "limits" | "domain" | "privacy" | "embeddings";

interface SettingsData {
  system_prompt: string;
  default_system_prompt: string;
  confidence_threshold: number;
  crawl_schedule: { frequency: "daily" | "weekly" | "off"; time: string; weekday: number };
  timezone: string;
  rate_limits: { chat_per_ip_per_minute: number; chat_per_client_per_minute: number; login_per_ip_per_15min: number };
  public_domain: string;
  chat_notice: string;
  retention_days: number;
  mode: string;
  embedding: { model: string; dim: number | null; status: string; error: string | null; pending_model: string | null };
  embedding_job: Job | null;
}

export function Settings() {
  const [params, setParams] = useSearchParams();
  const tab = (params.get("tab") as Tab) || "models";
  const [data, setData] = useState<SettingsData | null>(null);
  const load = useCallback(() => api<SettingsData>("/api/admin/settings").then(setData), []);
  useEffect(() => {
    load();
  }, [load]);

  const tabs: { key: Tab; label: string }[] = [
    { key: "models", label: "AI models" },
    { key: "answers", label: "Answers & prompt" },
    { key: "crawling", label: "Crawl schedule" },
    { key: "limits", label: "Rate limits" },
    { key: "domain", label: "Public domain" },
    { key: "privacy", label: "Privacy" },
    { key: "embeddings", label: "Embeddings" },
  ];

  return (
    <div className="page">
      <PageHead title="Settings" description="System-wide configuration. Changes apply immediately, without a restart." />
      <Tabs tabs={tabs} value={tab} onChange={(t) => setParams({ tab: t })} />
      {tab === "models" ? <ModelsTab /> : !data ? <Loading /> : (
        <>
          {tab === "answers" && <AnswersTab data={data} onSaved={setData} />}
          {tab === "crawling" && <CrawlTab data={data} onSaved={setData} />}
          {tab === "limits" && <LimitsTab data={data} onSaved={setData} />}
          {tab === "domain" && <DomainTab data={data} onSaved={setData} />}
          {tab === "privacy" && <PrivacyTab data={data} onSaved={setData} />}
          {tab === "embeddings" && <EmbeddingsTab data={data} reload={load} />}
        </>
      )}
    </div>
  );
}

function useSave(onSaved: (d: SettingsData) => void) {
  const { busy, run } = useAction();
  const save = (body: Partial<SettingsData>) => run(async () => onSaved(await api<SettingsData>("/api/admin/settings", { method: "PUT", body })), "Settings saved");
  return { busy, save };
}

// ---------------------------------------------------------------- AI models
interface ModelsData {
  models: AIModel[];
  providers: Provider[];
  default_model_id: number | null;
  fallback_model_id: number | null;
  mode: string;
}

function ModelsTab() {
  const [data, setData] = useState<ModelsData | null>(null);
  const [editing, setEditing] = useState<AIModel | "new" | null>(null);
  const { busy, run } = useAction();
  const load = () => api<ModelsData>("/api/admin/models").then(setData);
  useEffect(() => {
    load();
  }, []);
  if (!data) return <Loading />;

  return (
    <div className="stack">
      <Card
        title="Saved models"
        subtitle="All model calls go through LiteLLM, so any provider works. Switching models never needs a restart."
        actions={<Button kind="primary" icon="plus" onClick={() => setEditing("new")}>Add model</Button>}
        bodyless
      >
        {data.models.length === 0 ? <Empty title="No models yet" /> : (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Name</th><th>Provider</th><th>API key</th><th>Last test</th><th>Role</th><th /></tr></thead>
              <tbody>
                {data.models.map((m) => (
                  <tr key={m.id}>
                    <td><strong>{m.name}</strong><div className="small muted mono">{m.model_name}</div></td>
                    <td className="small">{m.provider_label}{m.cloud ? <div><Badge>Cloud</Badge></div> : <div><Badge color="teal">Local</Badge></div>}</td>
                    <td className="small mono">{m.api_key_unreadable ? <Badge color="red">Unreadable (SECRET_KEY changed?)</Badge> : m.api_key_masked || <span className="muted">—</span>}</td>
                    <td className="small">{m.last_test_ok === null ? <span className="muted">Never</span> : <Badge color={m.last_test_ok ? "green" : "red"}><span className="dot" />{m.last_test_ok ? "OK" : "Failed"} · {relative(m.last_test_at)}</Badge>}</td>
                    <td>{m.is_default && <Badge color="teal">Default</Badge>} {m.is_fallback && <Badge color="blue">Fallback</Badge>}</td>
                    <td className="num nowrap">
                      {!m.is_default && <Button size="sm" disabled={busy} onClick={() => run(async () => setData(await api(`/api/admin/models/${m.id}/default`, { method: "POST" })), "Default model changed")}>Make default</Button>}{" "}
                      <Button size="sm" onClick={() => setEditing(m)}>Edit</Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card title="Fallback model" subtitle="Used automatically when a client's model fails or times out.">
        <div className="row">
          <select className="select" style={{ maxWidth: 360 }} value={data.fallback_model_id ?? ""} aria-label="Fallback model"
            onChange={(e) => run(async () => setData(await api("/api/admin/models/fallback", { body: { model_id: e.target.value ? Number(e.target.value) : null } })), "Fallback updated")}>
            <option value="">No fallback</option>
            {data.models.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
          </select>
        </div>
      </Card>

      {editing && (
        <ModelEditor
          model={editing === "new" ? null : editing}
          providers={data.providers}
          mode={data.mode}
          onClose={() => setEditing(null)}
          onSaved={(d) => { setData(d); setEditing(null); }}
        />
      )}
    </div>
  );
}

function ModelEditor({ model, providers, mode, onClose, onSaved }: { model: AIModel | null; providers: Provider[]; mode: string; onClose: () => void; onSaved: (d: ModelsData) => void }) {
  const [values, setValues] = useState<ModelValues>(model ? modelToValues(model) : emptyModel(providers.find((p) => p.key === (mode === "onprem" ? "bionic" : "openai"))));
  const [test, setTest] = useState<TestResult | null>(null);
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const runTest = async () => {
    setTesting(true);
    setTest(null);
    try {
      setTest(await api<TestResult>("/api/admin/models/test", { body: { model_id: model?.id ?? null, config: payload(values) } }));
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
      const d = model
        ? await api<ModelsData>(`/api/admin/models/${model.id}`, { method: "PUT", body: payload(values) })
        : await api<ModelsData>("/api/admin/models", { body: payload(values) });
      onSaved(d);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };
  const remove = async () => {
    try {
      onSaved(await api<ModelsData>(`/api/admin/models/${model!.id}`, { method: "DELETE" }));
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <Modal
      title={model ? `Edit ${model.name}` : "Add model"}
      onClose={onClose}
      wide
      footer={
        <>
          {model && (confirmDelete ? <Button kind="danger" onClick={remove}>Really delete?</Button> : <Button kind="danger" icon="trash" onClick={() => setConfirmDelete(true)}>Delete</Button>)}
          <span className="spacer" />
          <Button onClick={onClose}>Cancel</Button>
          <Button kind="primary" loading={saving} onClick={save}>Save</Button>
        </>
      }
    >
      <div className="stack">
        {error && <Alert kind="error">{error}</Alert>}
        <ModelForm values={values} onChange={(v) => { setValues(v); setTest(null); }} providers={providers} mode={mode} editing={!!model} hasStoredKey={model?.has_api_key} onTest={runTest} testResult={test} testing={testing} />
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------- answers
function AnswersTab({ data, onSaved }: { data: SettingsData; onSaved: (d: SettingsData) => void }) {
  const [prompt, setPrompt] = useState(data.system_prompt);
  const [threshold, setThreshold] = useState(data.confidence_threshold);
  const { busy, save } = useSave(onSaved);
  const { busy: resetting, run } = useAction();
  return (
    <div className="stack">
      <Card title="Answer confidence threshold" subtitle="Questions whose best matching content scores below this are logged as unanswered.">
        <div className="row">
          <input type="range" min={0} max={1} step={0.01} value={threshold} onChange={(e) => setThreshold(Number(e.target.value))} style={{ width: 280 }} aria-label="Confidence threshold" />
          <input className="input" type="number" min={0} max={1} step={0.01} value={threshold} onChange={(e) => setThreshold(Number(e.target.value))} style={{ width: 90 }} aria-label="Confidence threshold value" />
          <Button kind="primary" loading={busy} onClick={() => save({ confidence_threshold: threshold })}>Save</Button>
        </div>
        <p className="hint" style={{ marginTop: 8 }}>Tip: the Test chat tab shows the confidence score of each answer. With bge-m3, relevant matches usually score 0.55–0.8.</p>
      </Card>
      <Card
        title="System prompt"
        subtitle={<>Placeholders: <code>{"{bot_name}"}</code> <code>{"{client_name}"}</code> <code>{"{today}"}</code> <code>{"{context}"}</code>. Keep the <code>[[NO_ANSWER]]</code> rule so unanswered questions are detected.</>}
        actions={<Button loading={resetting} onClick={() => run(async () => { const d = await api<SettingsData>("/api/admin/settings/system-prompt/reset", { method: "POST" }); onSaved(d); setPrompt(d.system_prompt); }, "Prompt reset to default")}>Reset to default</Button>}
      >
        <div className="stack">
          <textarea className="textarea code" value={prompt} onChange={(e) => setPrompt(e.target.value)} aria-label="System prompt" />
          <div className="row end"><Button kind="primary" loading={busy} onClick={() => save({ system_prompt: prompt })}>Save prompt</Button></div>
        </div>
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------- crawl schedule
const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

function CrawlTab({ data, onSaved }: { data: SettingsData; onSaved: (d: SettingsData) => void }) {
  const [schedule, setSchedule] = useState(data.crawl_schedule);
  const [tz, setTz] = useState(data.timezone);
  const { busy, save } = useSave(onSaved);
  return (
    <Card title="Automatic re-crawl" subtitle="Re-crawls every active client's website and re-indexes their documents. Only changed pages are re-indexed.">
      <div className="form-grid">
        <Field label="Frequency" htmlFor="cs-freq">
          <select id="cs-freq" className="select" value={schedule.frequency} onChange={(e) => setSchedule({ ...schedule, frequency: e.target.value as SettingsData["crawl_schedule"]["frequency"] })}>
            <option value="daily">Daily</option><option value="weekly">Weekly</option><option value="off">Off</option>
          </select>
        </Field>
        {schedule.frequency === "weekly" && (
          <Field label="Day" htmlFor="cs-day">
            <select id="cs-day" className="select" value={schedule.weekday} onChange={(e) => setSchedule({ ...schedule, weekday: Number(e.target.value) })}>
              {WEEKDAYS.map((d, i) => <option key={d} value={i}>{d}</option>)}
            </select>
          </Field>
        )}
        <Field label="Time of day" htmlFor="cs-time"><input id="cs-time" className="input" type="time" value={schedule.time} onChange={(e) => setSchedule({ ...schedule, time: e.target.value })} disabled={schedule.frequency === "off"} /></Field>
        <Field label="Time zone" htmlFor="cs-tz" hint="IANA name, e.g. Asia/Kolkata, Europe/London, UTC."><input id="cs-tz" className="input" value={tz} onChange={(e) => setTz(e.target.value)} /></Field>
      </div>
      <div className="row end" style={{ marginTop: 14 }}><Button kind="primary" loading={busy} onClick={() => save({ crawl_schedule: schedule, timezone: tz })}>Save</Button></div>
    </Card>
  );
}

// ---------------------------------------------------------------- rate limits
function LimitsTab({ data, onSaved }: { data: SettingsData; onSaved: (d: SettingsData) => void }) {
  const [limits, setLimits] = useState(data.rate_limits);
  const { busy, save } = useSave(onSaved);
  const field = (key: keyof SettingsData["rate_limits"], label: string, hint: string) => (
    <Field label={label} htmlFor={`rl-${key}`} hint={hint}>
      <input id={`rl-${key}`} className="input" type="number" min={0} value={limits[key]} onChange={(e) => setLimits({ ...limits, [key]: Number(e.target.value) })} />
    </Field>
  );
  return (
    <Card title="Rate limits" subtitle="Protect against abuse and runaway costs. 0 disables a limit.">
      <div className="form-grid">
        {field("chat_per_ip_per_minute", "Chat messages per visitor IP / minute", "Typical: 10–30.")}
        {field("chat_per_client_per_minute", "Chat messages per client / minute", "Across all visitors of one website.")}
        {field("login_per_ip_per_15min", "Admin sign-in attempts per IP / 15 minutes", "Slows down password guessing.")}
      </div>
      <div className="row end" style={{ marginTop: 14 }}><Button kind="primary" loading={busy} onClick={() => save({ rate_limits: limits })}>Save</Button></div>
    </Card>
  );
}

// ---------------------------------------------------------------- domain
function DomainTab({ data, onSaved }: { data: SettingsData; onSaved: (d: SettingsData) => void }) {
  const [domain, setDomain] = useState(data.public_domain);
  const { busy, save } = useSave(onSaved);
  return (
    <Card title="Public domain" subtitle="Where websites load the widget from. Used in every client's embed code.">
      <div className="stack">
        <Field label="Domain" htmlFor="pd" hint="e.g. chat.example.com: no http:// and no path. Point its DNS at this server and enable HTTPS (see README).">
          <input id="pd" className="input" value={domain} onChange={(e) => setDomain(e.target.value)} />
        </Field>
        <Alert kind="info">Mode: <strong>{data.mode === "onprem" ? "On-premise (single client)" : "Cloud (multi-client)"}</strong>. The mode is chosen during setup.</Alert>
        <div className="row end"><Button kind="primary" loading={busy} onClick={() => save({ public_domain: domain })}>Save</Button></div>
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------- privacy
function PrivacyTab({ data, onSaved }: { data: SettingsData; onSaved: (d: SettingsData) => void }) {
  const [notice, setNotice] = useState(data.chat_notice);
  const [days, setDays] = useState(data.retention_days);
  const { busy, save } = useSave(onSaved);
  return (
    <Card title="Privacy" subtitle="Phone numbers and email addresses typed by visitors are always masked before anything is stored or sent to a model.">
      <div className="form-grid">
        <Field label="Chat notice" htmlFor="pv-notice" full hint="Shown under the chat input in every widget."><input id="pv-notice" className="input" value={notice} onChange={(e) => setNotice(e.target.value)} /></Field>
        <Field label="Keep questions for (days)" htmlFor="pv-days" hint="Older questions are deleted automatically every day. 0 = keep forever."><input id="pv-days" className="input" type="number" min={0} max={3650} value={days} onChange={(e) => setDays(Number(e.target.value))} /></Field>
      </div>
      <div className="row end" style={{ marginTop: 14 }}><Button kind="primary" loading={busy} onClick={() => save({ chat_notice: notice, retention_days: days })}>Save</Button></div>
    </Card>
  );
}

// ---------------------------------------------------------------- embeddings
function EmbeddingsTab({ data, reload }: { data: SettingsData; reload: () => void }) {
  const [model, setModel] = useState(data.embedding.pending_model || data.embedding.model);
  const [confirm, setConfirm] = useState(false);
  const { busy, run } = useAction();
  const downloading = data.embedding.status === "downloading";
  const changing = model.trim() !== data.embedding.model;
  return (
    <Card title="Embedding model" subtitle="Converts content and questions into vectors for semantic search.">
      <div className="stack">
        <dl className="kv">
          <dt>Current model</dt><dd className="mono">{data.embedding.model}</dd>
          <dt>Status</dt><dd><Badge color={data.embedding.status === "ready" ? "green" : downloading ? "blue" : "red"}>{data.embedding.status}</Badge></dd>
          <dt>Vector size</dt><dd>{data.embedding.dim ?? "—"}</dd>
        </dl>
        {data.embedding.error && <Alert kind="error">{data.embedding.error}</Alert>}
        {downloading && data.embedding_job && <JobProgress jobId={data.embedding_job.id} onDone={reload} />}
        <Field label="Hugging Face model id" htmlFor="emb" hint="Multilingual models such as BAAI/bge-m3 or intfloat/multilingual-e5-large work well.">
          <input id="emb" className="input mono" value={model} onChange={(e) => { setModel(e.target.value); setConfirm(false); }} disabled={downloading} />
        </Field>
        {changing && <Alert kind="warning">Switching the embedding model re-indexes every client's pages and documents. Search quality may change while that runs.</Alert>}
        <div className="row end">
          {data.embedding.status !== "ready" && !changing && !downloading && <Button kind="primary" loading={busy} onClick={() => run(async () => { await api("/api/admin/settings/embedding", { body: { model } }); reload(); })}>Download</Button>}
          {changing && !downloading && (confirm
            ? <Button kind="danger" loading={busy} onClick={() => run(async () => { await api("/api/admin/settings/embedding", { body: { model } }); reload(); }, "Download started")}>Yes, switch and re-index</Button>
            : <Button kind="primary" onClick={() => setConfirm(true)}>Switch model…</Button>)}
        </div>
      </div>
    </Card>
  );
}
