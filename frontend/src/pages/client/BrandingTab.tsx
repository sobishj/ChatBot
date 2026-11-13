// Branding with a live preview rendered by the real widget.js (preview mode).
import { useEffect, useMemo, useRef, useState } from "react";
import { api, type Branding, type BubbleIcon, type BubbleShape } from "../../api";
import { Button, Card, Field, useAction } from "../../components/ui";
import type { TabProps } from "./ClientDetail";

const BUBBLE_SHAPES: [BubbleShape, string][] = [["speech", "Speech bubble"], ["message", "Message"], ["round", "Circle"]];

/** A small drawing of the chat button's shape (the widget draws the real one with CSS). */
function ShapePreview({ shape, color }: { shape: BubbleShape; color: string }) {
  const body = shape === "message"
    ? <rect x="2" y="4" width="36" height="28" rx="9" fill={color} />
    : <circle cx="20" cy="18" r="16" fill={color} />;
  const tail = shape === "speech" ? <path d="M26 30 L36 36 L32 26 Z" fill={color} /> : shape === "message" ? <path d="M26 31 L34 39 L33 31 Z" fill={color} /> : null;
  return (
    <svg width="40" height="40" viewBox="0 0 40 40" aria-hidden="true">
      {body}
      {tail}
      <path d="M13 17h14M13 22h9" stroke="#fff" strokeWidth="2.4" strokeLinecap="round" />
    </svg>
  );
}

// Same shapes as the widget's BUBBLE_ICONS (app/widget/widget.js).
const BUBBLE_ICON_CHOICES: [BubbleIcon, string, string][] = [
  ["chat", "Chat", '<path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/>'],
  ["message", "Message", '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/><path d="M8 10h.01M12 10h.01M16 10h.01"/>'],
  ["headset", "Support", '<path d="M3 14v-2a9 9 0 0 1 18 0v2"/><path d="M21 15a2 2 0 0 1-2 2h-1v-6h1a2 2 0 0 1 2 2zM3 15a2 2 0 0 0 2 2h1v-6H5a2 2 0 0 0-2 2z"/>'],
  ["help", "Help", '<circle cx="12" cy="12" r="9"/><path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 2.5-3 4.5M12 17.5h.01"/>'],
  ["sparkle", "AI", '<path d="M12 3l1.8 4.6 4.7 1.9-4.7 1.8L12 16l-1.8-4.7-4.7-1.8 4.7-1.9z"/><path d="M19 15l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z"/>'],
];

declare global {
  interface Window {
    WebsiteAssistant?: { mount: (o: { config: object; container?: HTMLElement; preview?: boolean; open?: boolean; apiBase?: string; device?: Device }) => WidgetHandle };
  }
}

type Device = "desktop" | "mobile";
type WidgetHandle = { update: (c: object) => void; setDevice: (d: Device) => void; destroy: () => void };

// The preview draws the widget on a stand-in screen of this size, scaled to fit the panel,
// so the size percentages look the way they will on a real screen.
const SCREENS: Record<Device, { w: number; h: number; label: string }> = {
  desktop: { w: 1280, h: 800, label: "a 1280 × 800 desktop screen" },
  mobile: { w: 390, h: 844, label: "a 390 × 844 phone" },
};
const MAX_PREVIEW_HEIGHT = 640;

function timeZones(): string[] {
  try {
    return (Intl as unknown as { supportedValuesOf: (k: string) => string[] }).supportedValuesOf("timeZone");
  } catch {
    return ["UTC", "Asia/Kolkata", "Asia/Dubai", "Europe/London", "America/New_York"];
  }
}

function SizeSlider({ id, label, value, onChange }: { id: string; label: string; value: number; onChange: (v: number) => void }) {
  return (
    <div className="field">
      <label htmlFor={id}>{label}: <strong>{value}%</strong></label>
      <input id={id} type="range" min={20} max={100} step={1} value={value} onChange={(e) => onChange(Number(e.target.value))} />
    </div>
  );
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
  const [globalTz, setGlobalTz] = useState("UTC");
  const [device, setDevice] = useState<Device>("desktop");
  const [frameWidth, setFrameWidth] = useState(420);
  const measure = useRef<HTMLDivElement>(null);
  const zones = useMemo(timeZones, []);
  const { busy, run } = useAction();
  const stage = useRef<HTMLDivElement>(null);
  const handle = useRef<WidgetHandle | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const bubbleInput = useRef<HTMLInputElement>(null);
  const set = <K extends keyof Branding>(k: K, v: Branding[K]) => setB({ ...b, [k]: v });

  useEffect(() => {
    api<{ chat_notice?: string; timezone?: string }>("/api/admin/settings")
      .then((s) => { setNotice(s.chat_notice ?? ""); setGlobalTz(s.timezone || "UTC"); })
      .catch(() => setNotice("Chats are recorded to improve service."));
  }, []);

  useEffect(() => {
    const el = measure.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => setFrameWidth(entry.contentRect.width));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const screen = SCREENS[device];
  const scale = Math.min(frameWidth / screen.w, MAX_PREVIEW_HEIGHT / screen.h);

  const previewConfig = {
    client_id: `preview-${client.client_id}`,
    name: client.name,
    bot_name: b.bot_name,
    primary_color: b.primary_color,
    greeting: b.greeting,
    default_language: b.default_language,
    position: b.position,
    width_pct: b.width_pct,
    height_pct: b.height_pct,
    mobile_width_pct: b.mobile_width_pct,
    mobile_height_pct: b.mobile_height_pct,
    logo_url: client.logo_url ? `${location.origin}${client.logo_url}` : null,
    bubble_icon: b.bubble_icon ?? "chat",
    bubble_shape: b.bubble_shape ?? "speech",
    bubble_icon_url: client.bubble_icon_url ? `${location.origin}${client.bubble_icon_url}` : null,
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
      else handle.current = window.WebsiteAssistant.mount({ config: previewConfig, container: stage.current, preview: true, open: true, device });
    });
    return () => { cancelled = true; };
  });
  useEffect(() => { handle.current?.setDevice(device); }, [device]);
  useEffect(() => () => { handle.current?.destroy(); handle.current = null; }, []);

  const uploadBubbleIcon = (file: File) => run(async () => {
    const form = new FormData();
    form.append("file", file);
    await api(`/api/admin/clients/${client.client_id}/bubble-icon`, { form });
    await reload();
    setB((cur) => ({ ...cur, bubble_icon: "image" }));
  }, "Bubble icon updated");

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
            <span className="label">Chat window size</span>
            <span className="hint">Percent of the visitor's screen. On desktops the window never gets smaller than 300 × 360 pixels so it stays usable.</span>
          </div>
          <SizeSlider id="b-w" label="Desktop width" value={b.width_pct} onChange={(v) => set("width_pct", v)} />
          <SizeSlider id="b-h" label="Desktop height" value={b.height_pct} onChange={(v) => set("height_pct", v)} />
          <SizeSlider id="b-mw" label="Phone width" value={b.mobile_width_pct} onChange={(v) => set("mobile_width_pct", v)} />
          <SizeSlider id="b-mh" label="Phone height" value={b.mobile_height_pct} onChange={(v) => set("mobile_height_pct", v)} />
          <span className="hint full">On phones (screens up to 480 pixels wide) the window is centred at the bottom. Set both to 100% for full screen.</span>
          <Field label="Time zone" htmlFor="b-tz" full hint="Used when visitors ask things like “what time is it?” or “are you open now?”.">
            <select id="b-tz" className="select" value={b.timezone} onChange={(e) => set("timezone", e.target.value)}>
              <option value="">Same as the global setting ({globalTz})</option>
              {b.timezone && !zones.includes(b.timezone) && <option value={b.timezone}>{b.timezone}</option>}
              {zones.map((z) => <option key={z} value={z}>{z.replace(/_/g, " ")}</option>)}
            </select>
          </Field>
          <Field label="Contact details" htmlFor="b-contact" full hint="Given to visitors when the assistant can't answer their question. Leave empty to use contact details found on the website.">
            <textarea id="b-contact" className="textarea" rows={3} maxLength={500} value={b.contact_details} onChange={(e) => set("contact_details", e.target.value)} placeholder={"Phone: +91 98765 43210\nEmail: info@example.com\nWhatsApp: +91 98765 43210"} />
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
          <div className="field full">
            <span className="label">Chat button shape</span>
            <div className="row" role="radiogroup" aria-label="Chat button shape">
              {BUBBLE_SHAPES.map(([key, label]) => {
                const chosen = (b.bubble_shape ?? "speech") === key;
                return (
                  <button key={key} type="button" role="radio" aria-checked={chosen} onClick={() => set("bubble_shape", key)}
                    style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 6, padding: "10px 14px 8px", borderRadius: 10, cursor: "pointer", background: "var(--surface)", border: chosen ? "2px solid var(--accent)" : "1px solid var(--border)" }}>
                    <ShapePreview shape={key} color={b.primary_color} />
                    <span className="small">{label}</span>
                  </button>
                );
              })}
            </div>
          </div>
          <div className="field full">
            <span className="label">Chat bubble icon</span>
            <div className="row" role="radiogroup" aria-label="Chat bubble icon">
              {BUBBLE_ICON_CHOICES.map(([key, label, paths]) => (
                <button key={key} type="button" role="radio" aria-checked={(b.bubble_icon ?? "chat") === key} title={label}
                  onClick={() => set("bubble_icon", key)}
                  style={{ width: 48, height: 48, borderRadius: "50%", border: (b.bubble_icon ?? "chat") === key ? "3px solid var(--accent)" : "1px solid var(--border)", background: b.primary_color, color: "#fff", display: "grid", placeItems: "center", cursor: "pointer" }}>
                  <svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" dangerouslySetInnerHTML={{ __html: paths }} />
                  <span className="sr-only">{label}</span>
                </button>
              ))}
              <button type="button" role="radio" aria-checked={b.bubble_icon === "image"} title="Your image" disabled={!client.bubble_icon_url}
                onClick={() => set("bubble_icon", "image")}
                style={{ width: 48, height: 48, borderRadius: "50%", border: b.bubble_icon === "image" ? "3px solid var(--accent)" : "1px solid var(--border)", background: b.primary_color, display: "grid", placeItems: "center", cursor: client.bubble_icon_url ? "pointer" : "not-allowed", opacity: client.bubble_icon_url ? 1 : 0.4 }}>
                {client.bubble_icon_url ? <img src={client.bubble_icon_url} alt="Your image" style={{ width: "62%", height: "62%", objectFit: "contain" }} /> : <span className="small" style={{ color: "#fff" }}>Img</span>}
              </button>
              <Button icon="upload" size="sm" onClick={() => bubbleInput.current?.click()} loading={busy}>Upload image</Button>
              {client.bubble_icon_url && <Button size="sm" kind="ghost" onClick={() => run(async () => { await api(`/api/admin/clients/${client.client_id}/bubble-icon`, { method: "DELETE" }); await reload(); setB((cur) => ({ ...cur, bubble_icon: cur.bubble_icon === "image" ? "chat" : cur.bubble_icon })); }, "Bubble image removed")}>Remove image</Button>}
              <input ref={bubbleInput} type="file" accept="image/png,image/jpeg,image/gif,image/webp" hidden onChange={(e) => { const f = e.target.files?.[0]; if (f) uploadBubbleIcon(f); e.target.value = ""; }} />
            </div>
            <span className="hint">The round button visitors click to open the chat. Pick an icon, or upload your own (PNG, JPG, GIF or WebP, up to 1 MB; a square image with a transparent background looks best). Click Save to apply an icon choice.</span>
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
        <div className="row between">
          <span className="label">Live preview</span>
          <div className="segmented" role="group" aria-label="Preview device">
            {(["desktop", "mobile"] as Device[]).map((d) => (
              <button key={d} type="button" className={device === d ? "active" : ""} aria-pressed={device === d} onClick={() => setDevice(d)}>{d === "desktop" ? "Desktop" : "Phone"}</button>
            ))}
          </div>
        </div>
        <div ref={measure}>
          <div className="preview-frame" style={{ width: screen.w * scale, height: screen.h * scale }}>
            <div className="preview-stage" ref={stage} style={{ width: screen.w, height: screen.h, transform: `scale(${scale})` }}>
              <div className="fake"><div style={{ width: "60%" }} /><div style={{ width: "85%" }} /><div style={{ width: "72%" }} /><div style={{ width: "40%" }} /></div>
            </div>
          </div>
        </div>
        <span className="hint">Shown on {screen.label}, scaled to fit. The preview uses the real widget. Messages here aren't sent to the model; use Test chat for real answers.</span>
      </div>
    </div>
  );
}
