// Branding with a live preview rendered by the real widget.js (preview mode).
import { useEffect, useRef, useState } from "react";
import { api, type Branding } from "../../api";
import { Button, Card, Field, useAction } from "../../components/ui";
import type { TabProps } from "./ClientDetail";

declare global {
  interface Window {
    WebsiteAssistant?: { mount: (o: { config: object; container?: HTMLElement; preview?: boolean; open?: boolean; apiBase?: string }) => { update: (c: object) => void; destroy: () => void } };
  }
}

const LANGUAGES = [
  ["en", "English"], ["ml", "Malayalam"], ["hi", "Hindi"], ["ta", "Tamil"], ["ar", "Arabic"],
  ["kn", "Kannada"], ["te", "Telugu"], ["fr", "French"], ["de", "German"], ["es", "Spanish"],
];

function loadWidgetScript(): Promise<void> {
  if (window.WebsiteAssistant) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = "/widget.js";
    s.onload = () => resolve();
    s.onerror = () => reject(new Error("widget.js failed to load"));
    document.head.appendChild(s);
  });
}

export function BrandingTab({ client, reload }: TabProps) {
  const [b, setB] = useState<Branding>(client.branding);
  const [notice, setNotice] = useState("");
  const { busy, run } = useAction();
  const stage = useRef<HTMLDivElement>(null);
  const handle = useRef<{ update: (c: object) => void; destroy: () => void } | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const set = <K extends keyof Branding>(k: K, v: Branding[K]) => setB({ ...b, [k]: v });

  useEffect(() => {
    api<{ chat_notice?: string }>("/api/admin/settings").then((s) => setNotice(s.chat_notice ?? "")).catch(() => setNotice("Chats are recorded to improve service."));
  }, []);

  const previewConfig = {
    client_id: `preview-${client.client_id}`,
    name: client.name,
    bot_name: b.bot_name,
    primary_color: b.primary_color,
    greeting: b.greeting,
    default_language: b.default_language,
    position: b.position,
    logo_url: client.logo_url ? `${location.origin}${client.logo_url}` : null,
    notice,
    powered_by: b.powered_by,
    powered_by_text: b.powered_by_text,
    powered_by_url: b.powered_by_url,
  };

  useEffect(() => {
    let cancelled = false;
    loadWidgetScript().then(() => {
      if (cancelled || !stage.current || !window.WebsiteAssistant) return;
      if (handle.current) handle.current.update(previewConfig);
      else handle.current = window.WebsiteAssistant.mount({ config: previewConfig, container: stage.current, preview: true, open: true });
    });
    return () => { cancelled = true; };
  });
  useEffect(() => () => { handle.current?.destroy(); handle.current = null; }, []);

  const uploadLogo = (file: File) => run(async () => {
    const form = new FormData();
    form.append("file", file);
    await api(`/api/admin/clients/${client.client_id}/logo`, { form });
    await reload();
  }, "Logo updated");

  return (
    <div className="grid split-preview">
      <Card title="Branding" subtitle="How the chat widget looks on the website.">
        <div className="form-grid">
          <Field label="Bot name" htmlFor="b-name"><input id="b-name" className="input" value={b.bot_name} onChange={(e) => set("bot_name", e.target.value)} /></Field>
          <Field label="Colour" htmlFor="b-color">
            <div className="color-row">
              <input type="color" aria-label="Pick colour" value={b.primary_color} onChange={(e) => set("primary_color", e.target.value)} />
              <input id="b-color" className="input mono" value={b.primary_color} onChange={(e) => set("primary_color", e.target.value)} />
            </div>
          </Field>
          <Field label="Greeting message" htmlFor="b-greet" full><textarea id="b-greet" className="textarea" rows={2} value={b.greeting} onChange={(e) => set("greeting", e.target.value)} /></Field>
          <Field label="Default language" htmlFor="b-lang" hint="Widget labels. The assistant always answers in the visitor's language.">
            <select id="b-lang" className="select" value={b.default_language} onChange={(e) => set("default_language", e.target.value)}>
              {LANGUAGES.map(([code, label]) => <option key={code} value={code}>{label}</option>)}
            </select>
          </Field>
          <Field label="Position" htmlFor="b-pos">
            <select id="b-pos" className="select" value={b.position} onChange={(e) => set("position", e.target.value as Branding["position"])}>
              <option value="right">Bottom right</option><option value="left">Bottom left</option>
            </select>
          </Field>
          <div className="field full">
            <span className="label">Logo</span>
            <div className="row">
              {client.logo_url ? <img src={client.logo_url} alt="Current logo" style={{ width: 44, height: 44, objectFit: "contain", border: "1px solid var(--border)", borderRadius: 8, padding: 3 }} /> : <span className="muted small">No logo</span>}
              <Button icon="upload" onClick={() => fileInput.current?.click()} loading={busy}>Upload</Button>
              {client.logo_url && <Button kind="ghost" onClick={() => run(async () => { await api(`/api/admin/clients/${client.client_id}/logo`, { method: "DELETE" }); await reload(); }, "Logo removed")}>Remove</Button>}
              <input ref={fileInput} type="file" accept="image/png,image/jpeg,image/gif,image/webp" hidden onChange={(e) => { const f = e.target.files?.[0]; if (f) uploadLogo(f); e.target.value = ""; }} />
            </div>
            <span className="hint">PNG, JPG, GIF or WebP, up to 1 MB. Square images look best.</span>
          </div>
          <label className="check full"><input type="checkbox" checked={b.powered_by} onChange={(e) => set("powered_by", e.target.checked)} />Show “Powered by” footer</label>
          {b.powered_by && (
            <>
              <Field label="Powered-by text" htmlFor="b-pt"><input id="b-pt" className="input" value={b.powered_by_text} onChange={(e) => set("powered_by_text", e.target.value)} /></Field>
              <Field label="Powered-by link (optional)" htmlFor="b-pu"><input id="b-pu" className="input" value={b.powered_by_url} onChange={(e) => set("powered_by_url", e.target.value)} placeholder="https://…" /></Field>
            </>
          )}
        </div>
        <div className="row end" style={{ marginTop: 16 }}>
          <Button onClick={() => setB(client.branding)}>Reset</Button>
          <Button kind="primary" loading={busy} onClick={() => run(async () => { await api(`/api/admin/clients/${client.client_id}/branding`, { method: "PUT", body: b }); await reload(); }, "Branding saved")}>Save</Button>
        </div>
      </Card>
      <div className="stack" style={{ gap: 8 }}>
        <span className="label">Live preview</span>
        <div className="preview-stage" ref={stage}>
          <div className="fake"><div style={{ width: "60%" }} /><div style={{ width: "85%" }} /><div style={{ width: "72%" }} /><div style={{ width: "40%" }} /></div>
        </div>
        <span className="hint">The preview uses the real widget. Messages here aren't sent to the model; use Test chat for real answers.</span>
      </div>
    </div>
  );
}
