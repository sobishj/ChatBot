// API actions: let the assistant use the client's own API (e.g. appointment booking).
// Off by default. client_admins see everything read-only (the API key is always masked);
// changes are super-admin only, which the API enforces as well.
import { useCallback, useEffect, useState } from "react";
import { api } from "../../api";
import { Alert, Badge, Button, Card, Empty, Field, Loading, Modal, useAction, useToast } from "../../components/ui";
import { duration, relative } from "../../format";
import type { TabProps } from "./ClientDetail";

const TYPES = ["string", "integer", "number", "boolean", "date", "phone", "email"] as const;
const LOCATIONS = ["query", "body", "path"] as const;
const METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"] as const;

interface Param { name: string; type: string; location: string; required: boolean; description: string; enum?: string[] }
interface Action { id: number; name: string; description: string; method: string; path: string; parameters: Param[]; requires_confirmation: boolean; enabled: boolean; response_hint: string }
interface ApiSettings {
  base_url: string; auth_type: "header" | "bearer" | "query"; auth_name: string; api_key_masked: string; has_api_key: boolean;
  api_key_unreadable: boolean; timeout_seconds: number; extra_headers: Record<string, string>; health_path: string;
}
interface ActionsData { enabled: boolean; settings: ApiSettings; actions: Action[]; can_edit: boolean; mode: string; allows_private: boolean }
interface CallResult { action?: string; ok: boolean; status_code: number | null; error: string | null; response_ms: number; response?: string | null; excerpt?: string; params?: Record<string, unknown> }
interface Call { id: number; action: string; channel: string; ok: boolean; status_code: number | null; error: string | null; response_ms: number; params: Record<string, unknown>; created_at: string }

export function ApiActionsTab({ client }: TabProps) {
  const [data, setData] = useState<ActionsData | null>(null);
  const { busy, run } = useAction();
  const base = `/api/admin/clients/${client.client_id}/api-actions`;
  const load = useCallback(() => api<ActionsData>(base).then(setData), [base]);
  useEffect(() => {
    load();
  }, [load]);

  if (!data) return <Loading />;
  const toggle = (enabled: boolean) =>
    run(async () => setData(await api<ActionsData>(`${base}/settings`, { method: "PUT", body: { enabled } })), enabled ? "API actions enabled" : "API actions disabled");

  return (
    <div className="stack">
      <Card>
        <div className="stack">
          <label className="check switch">
            <input type="checkbox" role="switch" checked={data.enabled} disabled={!data.can_edit || busy} onChange={(e) => toggle(e.target.checked)} />
            Enable API actions for this client
          </label>
          <p className="muted small" style={{ margin: 0 }}>
            Lets the assistant use this client's own API (e.g. appointment booking). Requires the client's API URL and key.
            {!data.can_edit && " Only a super admin can change this."}
          </p>
          {data.enabled && data.actions.filter((a) => a.enabled).length === 0 && (
            <Alert kind="info">No enabled actions yet, so chat works as before. Add actions below, or start from the appointment template.</Alert>
          )}
          {data.enabled && !data.settings.base_url && <Alert kind="warning">Set the API URL below: actions are not used until it is set.</Alert>}
        </div>
      </Card>
      {data.enabled && (
        <>
          <ConnectionCard base={base} data={data} onSaved={setData} />
          <ActionsCard base={base} data={data} onSaved={setData} />
          <CallsCard base={base} />
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- connection
function ConnectionCard({ base, data, onSaved }: { base: string; data: ActionsData; onSaved: (d: ActionsData) => void }) {
  const s = data.settings;
  const [url, setUrl] = useState(s.base_url);
  const [authType, setAuthType] = useState(s.auth_type);
  const [authName, setAuthName] = useState(s.auth_name);
  const [replacing, setReplacing] = useState(!s.has_api_key);
  const [key, setKey] = useState("");
  const [timeout, setTimeoutSeconds] = useState(s.timeout_seconds);
  const [healthPath, setHealthPath] = useState(s.health_path);
  const [headers, setHeaders] = useState<[string, string][]>(Object.entries(s.extra_headers));
  const [test, setTest] = useState<CallResult | null>(null);
  const { busy, run } = useAction();
  const { busy: testing, run: runTest } = useAction();
  const ro = !data.can_edit;

  const save = () => run(async () => {
    const d = await api<ActionsData>(`${base}/settings`, {
      method: "PUT",
      body: {
        base_url: url, auth_type: authType, auth_name: authName, api_key: replacing ? key : "", timeout_seconds: timeout,
        health_path: healthPath, extra_headers: Object.fromEntries(headers.filter(([k]) => k.trim())),
      },
    });
    onSaved(d);
    setKey("");
    setReplacing(!d.settings.has_api_key);
  }, "Connection saved");

  return (
    <Card title="Connection" subtitle="The client's API. The key is stored encrypted and is never shown again or sent to the browser or the widget.">
      <div className="stack">
        {data.allows_private && (
          <Alert kind="warning">On-premise mode: internal and private addresses (e.g. http://10.0.0.5 or host.docker.internal) are allowed. Make sure this URL points only at the client's API.</Alert>
        )}
        {s.api_key_unreadable && <Alert kind="error">The saved key can't be decrypted (was SECRET_KEY changed?). Enter it again.</Alert>}
        <div className="form-grid">
          <Field label="API URL" htmlFor="aa-url" full hint={data.allows_private ? "Base URL; action paths are added to it." : "Must be https://. Action paths are added to it."}>
            <input id="aa-url" className="input mono" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://api.example.com/v1" disabled={ro} />
          </Field>
          <Field label="Authentication" htmlFor="aa-auth">
            <select id="aa-auth" className="select" value={authType} onChange={(e) => setAuthType(e.target.value as ApiSettings["auth_type"])} disabled={ro}>
              <option value="header">Header (e.g. X-API-Key)</option>
              <option value="bearer">Bearer token (Authorization header)</option>
              <option value="query">Query parameter (e.g. ?api_key=)</option>
            </select>
          </Field>
          {authType !== "bearer" && (
            <Field label={authType === "query" ? "Parameter name" : "Header name"} htmlFor="aa-name">
              <input id="aa-name" className="input mono" value={authName} onChange={(e) => setAuthName(e.target.value)} disabled={ro} />
            </Field>
          )}
          <Field label="API key" htmlFor="aa-key" hint={replacing && s.has_api_key ? "Leave empty to keep the current key." : undefined}>
            {replacing ? (
              <input id="aa-key" className="input" type="password" autoComplete="new-password" value={key} onChange={(e) => setKey(e.target.value)} disabled={ro} />
            ) : (
              <div className="row">
                <span className="mono">{s.api_key_masked}</span>
                {!ro && <Button size="sm" onClick={() => setReplacing(true)}>Replace key</Button>}
              </div>
            )}
          </Field>
          <Field label="Timeout (seconds)" htmlFor="aa-timeout">
            <input id="aa-timeout" className="input" type="number" min={1} max={60} value={timeout} onChange={(e) => setTimeoutSeconds(Number(e.target.value))} disabled={ro} />
          </Field>
          <Field label="Health check path" htmlFor="aa-health" hint="Optional, used by Test connection, e.g. /health.">
            <input id="aa-health" className="input mono" value={healthPath} onChange={(e) => setHealthPath(e.target.value)} placeholder="/health" disabled={ro} />
          </Field>
        </div>
        <div className="stack" style={{ gap: 6 }}>
          <strong className="small">Extra headers</strong>
          {headers.map(([k, v], i) => (
            <div className="row" key={i}>
              <input className="input mono" style={{ maxWidth: 220 }} value={k} placeholder="Header" aria-label="Header name" disabled={ro}
                onChange={(e) => setHeaders(headers.map((h, j) => (j === i ? [e.target.value, h[1]] : h)))} />
              <input className="input mono" style={{ flex: 1 }} value={v} placeholder="Value" aria-label="Header value" disabled={ro}
                onChange={(e) => setHeaders(headers.map((h, j) => (j === i ? [h[0], e.target.value] : h)))} />
              {!ro && <Button size="sm" kind="ghost" icon="trash" aria-label="Remove header" onClick={() => setHeaders(headers.filter((_, j) => j !== i))} />}
            </div>
          ))}
          {!ro && <div><Button size="sm" icon="plus" onClick={() => setHeaders([...headers, ["", ""]])}>Add header</Button></div>}
        </div>
        {test && (
          test.ok
            ? <Alert kind="success">Connected · HTTP {test.status_code} · {duration(test.response_ms)}</Alert>
            : <Alert kind="error">{test.error}{test.status_code ? ` · ${duration(test.response_ms)}` : ""}{test.excerpt ? <div className="small mono" style={{ marginTop: 4 }}>{test.excerpt}</div> : null}</Alert>
        )}
        {!ro && (
          <div className="row end">
            <Button icon="play" loading={testing} disabled={!s.base_url} onClick={() => runTest(async () => setTest(await api<CallResult>(`${base}/test-connection`, { method: "POST" })))}>Test connection</Button>
            <Button kind="primary" loading={busy} onClick={save}>Save connection</Button>
          </div>
        )}
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------- actions
const emptyAction = (): Omit<Action, "id"> => ({ name: "", description: "", method: "GET", path: "/", parameters: [], requires_confirmation: false, enabled: true, response_hint: "" });

function ActionsCard({ base, data, onSaved }: { base: string; data: ActionsData; onSaved: (d: ActionsData) => void }) {
  const [editing, setEditing] = useState<Action | "new" | null>(null);
  const [testing, setTesting] = useState<Action | null>(null);
  const { busy, run } = useAction();
  const toast = useToast();
  const ro = !data.can_edit;
  const template = () => run(async () => {
    const d = await api<ActionsData & { added: string[] }>(`${base}/actions/template`, { method: "POST" });
    onSaved(d);
    toast(d.added.length ? `Added ${d.added.join(", ")}. Adjust paths and parameters to the client's API.` : "All template actions already exist.");
  });

  return (
    <Card
      title="Actions"
      subtitle="What the assistant may do with the API. Anything that changes data is always confirmed by the visitor first."
      actions={!ro && (
        <div className="row">
          <Button loading={busy} onClick={template}>Templates: appointment booking</Button>
          <Button kind="primary" icon="plus" onClick={() => setEditing("new")}>Add action</Button>
        </div>
      )}
      bodyless
    >
      {data.actions.length === 0 ? <Empty title="No actions yet">{ro ? "A super admin can add actions." : "Add one, or start from the appointment-booking template and adjust it to the client's API."}</Empty> : (
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>Name</th><th>Method</th><th>Path</th><th>Enabled</th><th>Confirmation</th><th /></tr></thead>
            <tbody>
              {data.actions.map((a) => (
                <tr key={a.id}>
                  <td><strong className="mono">{a.name}</strong><div className="small muted">{a.description}</div></td>
                  <td><Badge color={a.method === "GET" ? "blue" : "amber"}>{a.method}</Badge></td>
                  <td className="mono small">{a.path}</td>
                  <td>{a.enabled ? <Badge color="green">On</Badge> : <Badge>Off</Badge>}</td>
                  <td>{a.requires_confirmation ? "Visitor confirms" : "No"}</td>
                  <td className="num nowrap">
                    {!ro && <Button size="sm" icon="play" onClick={() => setTesting(a)}>Test</Button>}{" "}
                    {!ro ? <Button size="sm" onClick={() => setEditing(a)}>Edit</Button> : <Button size="sm" onClick={() => setEditing(a)}>View</Button>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {editing && <ActionForm base={base} action={editing === "new" ? null : editing} readOnly={ro} onClose={() => setEditing(null)} onSaved={(d) => { onSaved(d); setEditing(null); }} />}
      {testing && <TestAction base={base} action={testing} onClose={() => setTesting(null)} />}
    </Card>
  );
}

function ActionForm({ base, action, readOnly, onClose, onSaved }: { base: string; action: Action | null; readOnly: boolean; onClose: () => void; onSaved: (d: ActionsData) => void }) {
  const [a, setA] = useState<Omit<Action, "id">>(action ? { ...action, parameters: action.parameters.map((p) => ({ ...p })) } : emptyAction());
  const [enumText, setEnumText] = useState<string[]>(a.parameters.map((p) => (p.enum ?? []).join(", ")));
  const [confirmDelete, setConfirmDelete] = useState(false);
  const { busy, run } = useAction();
  const set = <K extends keyof Omit<Action, "id">>(k: K, v: Omit<Action, "id">[K]) => setA({ ...a, [k]: v });
  const setParam = (i: number, p: Partial<Param>) => set("parameters", a.parameters.map((x, j) => (j === i ? { ...x, ...p } : x)));
  const changes = a.method !== "GET";

  const save = () => run(async () => {
    const body = {
      ...a,
      requires_confirmation: changes || a.requires_confirmation,
      parameters: a.parameters.map((p, i) => ({ ...p, enum: enumText[i]?.split(",").map((x) => x.trim()).filter(Boolean) ?? [] })),
    };
    onSaved(action
      ? await api<ActionsData>(`${base}/actions/${action.id}`, { method: "PUT", body })
      : await api<ActionsData>(`${base}/actions`, { body }));
  }, "Action saved");
  const remove = () => run(async () => onSaved(await api<ActionsData>(`${base}/actions/${action!.id}`, { method: "DELETE" })), "Action deleted");

  return (
    <Modal
      title={action ? (readOnly ? action.name : `Edit ${action.name}`) : "Add action"}
      onClose={onClose}
      wide
      footer={readOnly ? <Button onClick={onClose}>Close</Button> : (
        <>
          {action && (confirmDelete ? <Button kind="danger" loading={busy} onClick={remove}>Really delete?</Button> : <Button kind="danger" icon="trash" onClick={() => setConfirmDelete(true)}>Delete</Button>)}
          <span className="spacer" />
          <Button onClick={onClose}>Cancel</Button>
          <Button kind="primary" loading={busy} onClick={save}>Save</Button>
        </>
      )}
    >
      <fieldset disabled={readOnly} style={{ border: 0, padding: 0, margin: 0 }} className="stack">
        <div className="form-grid">
          <Field label="Name" htmlFor="af-name" hint="snake_case, e.g. get_slots. The assistant sees this name.">
            <input id="af-name" className="input mono" value={a.name} onChange={(e) => set("name", e.target.value)} />
          </Field>
          <Field label="Method" htmlFor="af-method">
            <select id="af-method" className="select" value={a.method} onChange={(e) => set("method", e.target.value)}>
              {METHODS.map((m) => <option key={m}>{m}</option>)}
            </select>
          </Field>
          <Field label="Path" htmlFor="af-path" full hint="Relative to the API URL. Use {placeholders} for path parameters, e.g. /doctors/{doctor_id}/slots.">
            <input id="af-path" className="input mono" value={a.path} onChange={(e) => set("path", e.target.value)} />
          </Field>
          <Field label="Description" htmlFor="af-desc" full hint="Tells the assistant when to use this action.">
            <textarea id="af-desc" className="textarea" rows={2} value={a.description} onChange={(e) => set("description", e.target.value)} />
          </Field>
          <Field label="Response hint" htmlFor="af-hint" full hint="Optional: which response fields matter, e.g. “Returns booking_id; tell the visitor the booking ID.”">
            <input id="af-hint" className="input" value={a.response_hint} onChange={(e) => set("response_hint", e.target.value)} />
          </Field>
        </div>
        <div className="stack" style={{ gap: 6 }}>
          <strong className="small">Parameters</strong>
          {a.parameters.length > 0 && (
            <div className="param-row small muted"><span>Name</span><span>Type</span><span>Location</span><span>Required</span><span>Description</span><span>Allowed values</span><span /></div>
          )}
          {a.parameters.map((p, i) => (
            <div className="param-row" key={i}>
              <input className="input mono" value={p.name} aria-label="Parameter name" onChange={(e) => setParam(i, { name: e.target.value })} />
              <select className="select" value={p.type} aria-label="Type" onChange={(e) => setParam(i, { type: e.target.value })}>{TYPES.map((t) => <option key={t}>{t}</option>)}</select>
              <select className="select" value={p.location} aria-label="Location" onChange={(e) => setParam(i, { location: e.target.value })}>{LOCATIONS.map((l) => <option key={l}>{l}</option>)}</select>
              <label className="check"><input type="checkbox" checked={p.required || p.location === "path"} disabled={p.location === "path"} onChange={(e) => setParam(i, { required: e.target.checked })} aria-label="Required" /></label>
              <input className="input" value={p.description} placeholder="What it is" aria-label="Description" onChange={(e) => setParam(i, { description: e.target.value })} />
              <input className="input" value={enumText[i] ?? ""} placeholder="a, b, c (optional)" aria-label="Allowed values" onChange={(e) => setEnumText(enumText.map((x, j) => (j === i ? e.target.value : x)))} />
              {!readOnly ? <Button size="sm" kind="ghost" icon="trash" aria-label={`Remove ${p.name || "parameter"}`} onClick={() => { set("parameters", a.parameters.filter((_, j) => j !== i)); setEnumText(enumText.filter((_, j) => j !== i)); }} /> : <span />}
            </div>
          ))}
          {!readOnly && (
            <div><Button size="sm" icon="plus" onClick={() => { set("parameters", [...a.parameters, { name: "", type: "string", location: a.method === "GET" ? "query" : "body", required: true, description: "" }]); setEnumText([...enumText, ""]); }}>Add parameter</Button></div>
          )}
        </div>
        <label className="check">
          <input type="checkbox" checked={changes || a.requires_confirmation} disabled={changes} onChange={(e) => set("requires_confirmation", e.target.checked)} />
          Requires the visitor's confirmation{changes && " (always on for actions that change data)"}
        </label>
        <label className="check"><input type="checkbox" checked={a.enabled} onChange={(e) => set("enabled", e.target.checked)} />Enabled</label>
      </fieldset>
    </Modal>
  );
}

function TestAction({ base, action, onClose }: { base: string; action: Action; onClose: () => void }) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [live, setLive] = useState(false);
  const [result, setResult] = useState<CallResult | null>(null);
  const { busy, run } = useAction();
  const changes = action.method !== "GET";

  const go = () => run(async () => {
    const params: Record<string, unknown> = {};
    action.parameters.forEach((p) => {
      const v = values[p.name];
      if (v !== undefined && v !== "") params[p.name] = p.type === "boolean" ? v === "true" : v;
    });
    setResult(await api<CallResult>(`${base}/actions/${action.id}/test`, { body: { params, live } }));
  });

  return (
    <Modal title={`Test ${action.name}`} onClose={onClose} wide footer={<><Button onClick={onClose}>Close</Button><Button kind="primary" icon="play" loading={busy} disabled={changes && !live} onClick={go}>Run</Button></>}>
      <div className="stack">
        <p className="muted small" style={{ margin: 0 }}><Badge color={changes ? "amber" : "blue"}>{action.method}</Badge> <span className="mono">{action.path}</span></p>
        {action.parameters.length === 0 ? <p className="muted">No parameters.</p> : (
          <div className="form-grid">
            {action.parameters.map((p) => (
              <Field key={p.name} label={`${p.name}${p.required ? " *" : ""}`} htmlFor={`t-${p.name}`} hint={[p.type, p.description].filter(Boolean).join(" · ")}>
                {p.type === "boolean" ? (
                  <select id={`t-${p.name}`} className="select" value={values[p.name] ?? ""} onChange={(e) => setValues({ ...values, [p.name]: e.target.value })}>
                    <option value="" /><option value="true">true</option><option value="false">false</option>
                  </select>
                ) : p.enum && p.enum.length ? (
                  <select id={`t-${p.name}`} className="select" value={values[p.name] ?? ""} onChange={(e) => setValues({ ...values, [p.name]: e.target.value })}>
                    <option value="" />{p.enum.map((x) => <option key={x}>{x}</option>)}
                  </select>
                ) : (
                  <input id={`t-${p.name}`} className="input" type={p.type === "date" ? "date" : "text"} value={values[p.name] ?? ""} onChange={(e) => setValues({ ...values, [p.name]: e.target.value })} />
                )}
              </Field>
            ))}
          </div>
        )}
        {changes && (
          <Alert kind="warning">
            <label className="check"><input type="checkbox" checked={live} onChange={(e) => setLive(e.target.checked)} />I understand this will call the live API and may change real data.</label>
          </Alert>
        )}
        {result && (
          <div className="stack" style={{ gap: 6 }}>
            <div className="row">
              <Badge color={result.ok ? "green" : "red"}>{result.ok ? "OK" : "Failed"}</Badge>
              {result.status_code !== null && <span className="small">HTTP {result.status_code}</span>}
              <span className="small muted">{duration(result.response_ms)}</span>
            </div>
            {result.error && <Alert kind="error">{result.error}</Alert>}
            {result.response && <pre className="log-box">{result.response}</pre>}
          </div>
        )}
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------- audit log
function CallsCard({ base }: { base: string }) {
  const [page, setPage] = useState(1);
  const [data, setData] = useState<{ total: number; size: number; calls: Call[] } | null>(null);
  const load = useCallback(() => api<{ total: number; size: number; calls: Call[] }>(`${base}/calls?page=${page}&size=20`).then(setData), [base, page]);
  useEffect(() => {
    load();
  }, [load]);
  const pages = data ? Math.max(1, Math.ceil(data.total / data.size)) : 1;

  return (
    <Card title="Recent calls" subtitle="Every call to the client's API. Phone numbers and emails are masked." actions={<Button size="sm" icon="refresh" onClick={load}>Refresh</Button>} bodyless>
      {!data ? <Loading /> : data.calls.length === 0 ? <Empty title="No calls yet" /> : (
        <>
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>When</th><th>Action</th><th>Status</th><th className="num">Time</th><th>Parameters</th><th>Source</th></tr></thead>
              <tbody>
                {data.calls.map((c) => (
                  <tr key={c.id}>
                    <td className="small muted nowrap">{relative(c.created_at)}</td>
                    <td className="mono small">{c.action}</td>
                    <td>
                      <Badge color={c.ok ? "green" : "red"}>{c.status_code ?? (c.ok ? "OK" : "Error")}</Badge>
                      {c.error && <div className="small" style={{ color: "var(--danger)", maxWidth: 320 }}>{c.error}</div>}
                    </td>
                    <td className="num small">{duration(c.response_ms)}</td>
                    <td className="mono small" style={{ maxWidth: 320, overflowWrap: "anywhere" }}>{Object.keys(c.params).length ? JSON.stringify(c.params) : "—"}</td>
                    <td className="small">{c.channel === "admin" ? "Admin test" : c.channel === "test" ? "Test chat" : "Visitor"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {pages > 1 && (
            <div className="row end" style={{ padding: 12 }}>
              <Button size="sm" disabled={page <= 1} onClick={() => setPage(page - 1)}>Previous</Button>
              <span className="small muted">Page {page} of {pages}</span>
              <Button size="sm" disabled={page >= pages} onClick={() => setPage(page + 1)}>Next</Button>
            </div>
          )}
        </>
      )}
    </Card>
  );
}
