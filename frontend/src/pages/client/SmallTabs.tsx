// AI model, Allowed domains and Embed code tabs.
import { useEffect, useState } from "react";
import { api, type AIModel } from "../../api";
import { Alert, Badge, Button, Card, CopyButton, Field, Loading, useAction } from "../../components/ui";
import type { TabProps } from "./ClientDetail";

// ---------------------------------------------------------------- AI model
interface ModelChoice { ai_model_id: number | null; default_model_id: number | null; fallback_model_id: number | null; mode: string; models: AIModel[] }

export function ModelTab({ client, reload, isSuper }: TabProps) {
  const [data, setData] = useState<ModelChoice | null>(null);
  const [choice, setChoice] = useState<string>("");
  const { busy, run } = useAction();
  useEffect(() => {
    api<ModelChoice>(`/api/admin/clients/${client.client_id}/model`).then((d) => { setData(d); setChoice(d.ai_model_id ? String(d.ai_model_id) : ""); });
  }, [client.client_id]);
  if (!data) return <Loading />;
  const defaultModel = data.models.find((m) => m.id === data.default_model_id);
  const fallback = data.models.find((m) => m.id === data.fallback_model_id);
  const selected = data.models.find((m) => String(m.id) === choice) ?? defaultModel;

  return (
    <Card title="AI model" subtitle="Which saved model answers this client's visitors. Takes effect immediately.">
      <div className="stack">
        <Field label="Model" htmlFor="cm">
          <select id="cm" className="select" value={choice} onChange={(e) => setChoice(e.target.value)} disabled={!isSuper} style={{ maxWidth: 420 }}>
            <option value="">System default{defaultModel ? ` (${defaultModel.name})` : ""}</option>
            {data.models.map((m) => <option key={m.id} value={m.id}>{m.name} · {m.provider_label}</option>)}
          </select>
        </Field>
        {selected && (
          <dl className="kv">
            <dt>Provider</dt><dd>{selected.provider_label} {selected.cloud ? <Badge>Cloud</Badge> : <Badge color="teal">Local</Badge>}</dd>
            <dt>Model name</dt><dd className="mono">{selected.model_name}</dd>
            <dt>Fallback</dt><dd>{fallback ? fallback.name : "None (set one in Settings → AI models)"}</dd>
          </dl>
        )}
        {data.mode === "onprem" && selected?.cloud && <Alert kind="warning">This is a cloud model: visitor questions and content will be sent outside this server.</Alert>}
        {isSuper ? (
          <div className="row end">
            <Button kind="primary" loading={busy} onClick={() => run(async () => { await api(`/api/admin/clients/${client.client_id}/model`, { method: "PUT", body: { ai_model_id: choice ? Number(choice) : null } }); await reload(); }, "Model updated")}>Save</Button>
          </div>
        ) : <p className="hint">Only super admins can change the AI model.</p>}
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------- allowed domains
export function DomainsTab({ client, reload }: TabProps) {
  const [text, setText] = useState(client.allowed_domains.join("\n"));
  const { busy, run } = useAction();
  return (
    <Card title="Allowed domains" subtitle="Only pages on these websites can show this assistant. Requests from anywhere else are rejected.">
      <div className="stack">
        <Field label="Domains (one per line)" htmlFor="ad" hint={<><code>example.com</code> also allows <code>www.example.com</code>. <code>*.example.com</code> allows all subdomains. Add a port for local testing, e.g. <code>localhost:5500</code>.</>}>
          <textarea id="ad" className="textarea mono" rows={6} value={text} onChange={(e) => setText(e.target.value)} />
        </Field>
        {client.allowed_domains.length === 0 && <Alert kind="warning">No domains: the widget will not load anywhere.</Alert>}
        <div className="row end">
          <Button kind="primary" loading={busy} onClick={() => run(async () => { await api(`/api/admin/clients/${client.client_id}/domains`, { method: "PUT", body: { allowed_domains: text.split(/[\s,]+/).filter(Boolean) } }); await reload(); }, "Allowed domains saved")}>Save</Button>
        </div>
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------- embed code
interface EmbedData { snippet: string; widget_url: string; public_domain: string; allowed_domains: string[] }

const PLATFORMS: { name: string; steps: string[] }[] = [
  { name: "WordPress (WPCode plugin)", steps: ["Install and activate the free “WPCode – Insert Headers and Footers” plugin.", "Go to Code Snippets → Header & Footer.", "Paste the code into the Footer box and click Save Changes."] },
  { name: "Wix", steps: ["Open Settings → Custom Code (Advanced).", "Click + Add Custom Code, paste the code.", "Choose “All pages”, place it in Body – end, and Apply. (Requires a premium plan with a connected domain.)"] },
  { name: "Shopify", steps: ["Go to Online Store → Themes → … → Edit code.", "Open layout/theme.liquid.", "Paste the code just before </body> and Save."] },
  { name: "Squarespace", steps: ["Go to Settings → Advanced → Code Injection.", "Paste the code into the Footer box and Save. (Business plan or higher.)"] },
  { name: "Google Tag Manager", steps: ["Create a new Tag → Custom HTML and paste the code.", "Trigger: All Pages (or Initialization – All Pages).", "Save, then Submit / Publish the container."] },
  { name: "Custom HTML", steps: ["Paste the code just before the closing </body> tag on every page (or in your shared footer template)."] },
];

export function EmbedTab({ client }: TabProps) {
  const [data, setData] = useState<EmbedData | null>(null);
  const [open, setOpen] = useState<string | null>(PLATFORMS[0].name);
  useEffect(() => {
    api<EmbedData>(`/api/admin/clients/${client.client_id}/embed`).then(setData);
  }, [client.client_id]);
  if (!data) return <Loading />;
  return (
    <div className="stack">
      <Card title="Embed code" subtitle="Add this one line to every page of the website." actions={<CopyButton text={data.snippet} label="Copy code" />}>
        <div className="snippet">{data.snippet}</div>
        <div className="stack" style={{ marginTop: 14, gap: 8 }}>
          {!data.public_domain && <Alert kind="warning">No public domain is set (Settings → Public domain), so this code points at this server's address. Set a domain before going live.</Alert>}
          {data.allowed_domains.length === 0
            ? <Alert kind="warning">This client has no allowed domains yet; the widget will refuse to load.</Alert>
            : <p className="small muted" style={{ margin: 0 }}>The widget loads only on: {data.allowed_domains.join(", ")}.</p>}
        </div>
      </Card>
      <Card title="Installation guides" bodyless>
        {PLATFORMS.map((p) => (
          <div key={p.name} style={{ borderBottom: "1px solid var(--border)" }}>
            <button type="button" className="btn ghost" style={{ width: "100%", justifyContent: "space-between", borderRadius: 0, height: 46, padding: "0 20px" }} aria-expanded={open === p.name} onClick={() => setOpen(open === p.name ? null : p.name)}>
              <strong>{p.name}</strong><span className="muted">{open === p.name ? "−" : "+"}</span>
            </button>
            {open === p.name && (
              <ol style={{ margin: 0, padding: "0 20px 16px 40px" }}>
                {p.steps.map((s) => <li key={s} style={{ marginBottom: 4 }}>{s}</li>)}
              </ol>
            )}
          </div>
        ))}
      </Card>
    </div>
  );
}
