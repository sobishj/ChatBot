import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, type Job } from "../../api";
import { JobProgress } from "../../components/JobProgress";
import { Alert, Badge, Button, Card, Empty, Field, Loading, Modal, StatusBadge, useAction, useToast } from "../../components/ui";
import { Icon } from "../../components/icons";
import { approxTime, bytes, money, num, relative } from "../../format";
import type { TabProps } from "./ClientDetail";

interface Doc { id: number; source: "upload" | "folder"; path: string; filename: string; size_bytes: number; status: string; error: string | null; chunk_count: number; enabled: boolean; uploaded_at: string; indexed_at: string | null }
interface DocsData { documents: Doc[]; settings: { watch_path: string | null; scan_interval_minutes: number; last_scan_at: string | null; key_facts: string }; watched_root: string; mode: string }

/** Returned instead of starting indexing when the local embedding model would take very long. */
interface LargeUpload {
  pages: number;
  scanned: boolean;
  files: { filename: string; pages: number; scanned: boolean }[];
  current: { label: string; device: string; seconds: number; measured: boolean };
  cloud: { key: string; label: string; seconds: number; usd: number | null; recommended: boolean }[];
}

const ACCEPT = ".pdf,.docx,.xlsx,.txt,.md";

export function DocumentsTab({ client, reload, isSuper }: TabProps) {
  const [data, setData] = useState<DocsData | null>(null);
  const runningJob = client.active_jobs?.find((j) => j.type === "index_docs" || j.type === "scan_folder" || j.type === "reindex")?.id ?? null;
  const [jobId, setJobId] = useState<number | null>(runningJob);
  // Indexing continues in the background when you leave this tab; show it again on return.
  useEffect(() => {
    if (runningJob !== null) setJobId((current) => current ?? runningJob);
  }, [runningJob]);
  const [over, setOver] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [large, setLarge] = useState<LargeUpload | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const toast = useToast();
  const { busy, run } = useAction();

  const load = useCallback(() => api<DocsData>(`/api/admin/clients/${client.client_id}/documents`).then(setData), [client.client_id]);
  useEffect(() => {
    load();
  }, [load]);

  const upload = async (files: FileList | File[]) => {
    const list = Array.from(files);
    if (!list.length) return;
    const form = new FormData();
    list.forEach((f) => form.append("files", f));
    setUploading(true);
    try {
      const res = await api<{ saved: Doc[]; errors: string[]; job: Job | null; large: LargeUpload | null }>(`/api/admin/clients/${client.client_id}/documents`, { form });
      res.errors.forEach((e) => toast(e, "error"));
      if (res.saved.length && !res.large) toast(`${res.saved.length} file(s) uploaded, indexing…`);
      if (res.job) setJobId(res.job.id);
      if (res.large) setLarge(res.large);
      await load();
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setUploading(false);
    }
  };

  const startJob = (path: string) => run(async () => {
    const { job } = await api<{ job: Job }>(path, { method: "POST" });
    setJobId(job.id);
  });

  if (!data) return <Loading />;
  const uploads = data.documents.filter((d) => d.source === "upload");
  const folder = data.documents.filter((d) => d.source === "folder");
  const waiting = uploads.filter((d) => d.status === "pending");
  const indexPending = () => startJob(`/api/admin/clients/${client.client_id}/documents/index`);

  return (
    <div className="stack">
      <Card title="Upload documents" subtitle="PDF, Word (DOCX), Excel (XLSX), text and Markdown, up to 1 GB each. Scanned PDFs are read with OCR. Files are indexed automatically after upload.">
        <div
          className={`dropzone ${over ? "over" : ""}`}
          role="button"
          tabIndex={0}
          onClick={() => fileInput.current?.click()}
          onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && fileInput.current?.click()}
          onDragOver={(e) => { e.preventDefault(); setOver(true); }}
          onDragLeave={() => setOver(false)}
          onDrop={(e) => { e.preventDefault(); setOver(false); upload(e.dataTransfer.files); }}
        >
          <Icon name="upload" size={26} />
          <p style={{ margin: "8px 0 0" }}>{uploading ? "Uploading…" : <><strong>Drop files here</strong> or click to choose</>}</p>
          <input ref={fileInput} type="file" multiple accept={ACCEPT} hidden onChange={(e) => { if (e.target.files) upload(e.target.files); e.target.value = ""; }} />
        </div>
        {jobId && <div style={{ marginTop: 14 }}><JobProgress jobId={jobId} onDone={() => { setJobId(null); load(); reload(); }} /></div>}
        {!jobId && waiting.length > 0 && (
          <div style={{ marginTop: 14 }}>
            <Alert kind="info">
              <div className="row between">
                <span>{waiting.length} uploaded file(s) are waiting to be indexed.</span>
                <Button size="sm" kind="primary" loading={busy} onClick={indexPending}>Index now</Button>
              </div>
            </Alert>
          </div>
        )}
      </Card>

      {large && (
        <LargeUploadDialog
          info={large}
          isSuper={isSuper}
          onClose={() => { setLarge(null); load(); }}
          onIndexLocal={() => { setLarge(null); indexPending(); }}
        />
      )}

      <Card
        title={`Documents (${data.documents.length})`}
        actions={<Button icon="refresh" loading={busy} disabled={!!jobId || data.documents.length === 0} onClick={() => startJob(`/api/admin/clients/${client.client_id}/documents/reindex`)}>Re-index all</Button>}
        bodyless
      >
        {data.documents.length === 0 ? <Empty title="No documents yet">Upload files above{isSuper ? " or connect a watched folder below" : ""}.</Empty> : (
          <DocTable
            docs={[...uploads, ...folder]}
            onToggle={(d, enabled) => run(async () => {
              const r = await api<{ document: Doc }>(`/api/admin/clients/${client.client_id}/documents/${d.id}/enabled`, { method: "PUT", body: { enabled } });
              setData((cur) => cur && { ...cur, documents: cur.documents.map((x) => (x.id === d.id ? r.document : x)) });
            }, enabled ? `The assistant uses ${d.filename} again` : `The assistant no longer answers from ${d.filename}`)}
            onToggleAll={(enabled) => run(async () => {
              const r = await api<{ documents: Doc[] }>(`/api/admin/clients/${client.client_id}/documents/enabled`, { method: "PUT", body: { enabled } });
              setData((cur) => cur && { ...cur, documents: r.documents });
            }, enabled ? "The assistant uses all documents" : "The assistant no longer answers from any document")}
            onDelete={(d) => run(async () => { await api(`/api/admin/clients/${client.client_id}/documents/${d.id}`, { method: "DELETE" }); await load(); reload(); }, "Document deleted")} />
        )}
      </Card>

      <Card title="Answering">
        <div className="stack">
          <KeyFacts client={client} value={data.settings.key_facts} onSaved={(settings) => setData({ ...data, settings })} />
        </div>
      </Card>

      {(isSuper || data.settings.watch_path) && (
        <WatchedFolder client={client} data={data} isSuper={isSuper} onChanged={(job) => { if (job) setJobId(job.id); load(); }} onScan={() => startJob(`/api/admin/clients/${client.client_id}/documents/scan`)} scanning={!!jobId} />
      )}
    </div>
  );
}

function LargeUploadDialog({ info, isSuper, onClose, onIndexLocal }: { info: LargeUpload; isSuper: boolean; onClose: () => void; onIndexLocal: () => void }) {
  const navigate = useNavigate();
  const where = info.current.device === "cuda" ? "this server's GPU" : "this server's CPU";
  return (
    <Modal
      title="Large upload: choose how to index it"
      onClose={onClose}
      wide
      footer={
        <>
          <Button onClick={onClose}>Decide later</Button>
          <span className="spacer" />
          <Button onClick={onIndexLocal}>Index with {info.current.label} anyway</Button>
          {isSuper && <Button kind="primary" onClick={() => navigate("/settings?tab=embeddings")}>Choose a cloud model…</Button>}
        </>
      }
    >
      <div className="stack">
        <p style={{ margin: 0 }}>
          This upload has about <strong>{num(info.pages)} pages</strong>{info.scanned ? " of scanned images, which are read with OCR first" : ""}.
          With the current embedding model, <strong>{info.current.label}</strong> on {where}, indexing will take <strong>{approxTime(info.current.seconds)}</strong>
          {info.current.measured ? " (based on the speed measured on this server)" : " (estimate)"}.
        </p>
        <div>
          <strong>Cloud embedding models are much faster:</strong>
          <div className="table-wrap" style={{ marginTop: 8 }}>
            <table className="table">
              <thead><tr><th>Model</th><th>Time for this upload</th><th className="num">Approx. cost</th></tr></thead>
              <tbody>
                {info.cloud.map((c) => (
                  <tr key={c.key}>
                    <td>{c.label} {c.recommended && <Badge color="green">Recommended</Badge>}</td>
                    <td>{approxTime(c.seconds)}</td>
                    <td className="num">{c.usd != null ? money(c.usd) : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
        <Alert kind="info">
          A cloud model sends document text (and later, visitor questions) to the provider, and switching re-indexes all existing content once.
          {isSuper ? " Choosing one takes you to Settings → Embeddings; this upload is indexed automatically after the switch." : " Only a super admin can switch the embedding model (Settings → Embeddings)."}
        </Alert>
        <p className="small muted" style={{ margin: 0 }}>The files are saved either way. If you decide later, use <strong>Index now</strong> on this tab.</p>
      </div>
    </Modal>
  );
}

function KeyFacts({ client, value, onSaved }: { client: TabProps["client"]; value: string; onSaved: (s: DocsData["settings"]) => void }) {
  const [text, setText] = useState(value);
  const { busy, run } = useAction();
  return (
    <Field
      label="Key information (always given to the assistant)"
      htmlFor="key-facts"
      hint={`Facts the assistant must always know, whatever the visitor asks: ${client.name}'s opening hours, address, phone, parking… Keep it short. For "is it open now?" it uses the current time in the time zone set under Settings → Crawl schedule.`}
    >
      <textarea id="key-facts" className="textarea" rows={4} maxLength={4000} value={text} onChange={(e) => setText(e.target.value)}
        placeholder={"Open every day, 9:00 AM to 11:00 PM.\nAddress: …\nPhone: …"} />
      <div className="row end" style={{ marginTop: 8 }}>
        <Button kind="primary" size="sm" loading={busy} disabled={text === value}
          onClick={() => run(async () => onSaved((await api<{ settings: DocsData["settings"] }>(`/api/admin/clients/${client.client_id}/documents/answering`, { method: "PUT", body: { key_facts: text } })).settings), "Key information saved")}>
          Save key information
        </Button>
      </div>
    </Field>
  );
}

function DocTable({ docs, onToggle, onToggleAll, onDelete }: { docs: Doc[]; onToggle: (d: Doc, enabled: boolean) => void; onToggleAll: (enabled: boolean) => void; onDelete: (d: Doc) => void }) {
  const used = docs.filter((d) => d.enabled).length;
  const allBox = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (allBox.current) allBox.current.indeterminate = used > 0 && used < docs.length; // some ticked: show a dash
  }, [used, docs.length]);
  const [confirm, setConfirm] = useState<number | null>(null);
  return (
    <div className="table-wrap">
      <table className="table">
        <thead><tr><th title="Untick to stop the assistant answering from a document">
          <label className="check" style={{ gap: 6 }}>
            <input ref={allBox} type="checkbox" checked={docs.length > 0 && used === docs.length}
              onChange={(e) => onToggleAll(e.target.checked)} aria-label={used === docs.length ? "Ignore all documents" : "Use all documents"} />
            Use
          </label>
        </th><th>File</th><th>Source</th><th className="num">Size</th><th>Status</th><th>Indexed</th><th /></tr></thead>
        <tbody>
          {docs.map((d) => (
            <tr key={d.id} style={{ opacity: d.enabled ? 1 : 0.55 }}>
              <td style={{ width: 44 }}>
                <input type="checkbox" checked={d.enabled} onChange={(e) => onToggle(d, e.target.checked)}
                  aria-label={`Use ${d.filename} for answers`} title={d.enabled ? "Used for answers. Untick to ignore it." : "Ignored for answers. Tick to use it again."} />
              </td>
              <td style={{ maxWidth: 380 }}><strong className="truncate" title={d.path}>{d.filename}</strong>{d.source === "folder" && d.path !== d.filename && <span className="small muted truncate">{d.path}</span>}{d.error && <div className="small" style={{ color: "var(--danger)" }}>{d.error}</div>}</td>
              <td>{d.source === "upload" ? <Badge>Uploaded</Badge> : <Badge color="blue">Folder</Badge>}</td>
              <td className="num">{bytes(d.size_bytes)}</td>
              <td><StatusBadge status={d.status} />{d.status === "indexed" && <div className="small muted">{d.chunk_count} chunks</div>}</td>
              <td className="small muted nowrap">{relative(d.indexed_at)}</td>
              <td className="num">
                {d.source === "upload" && (confirm === d.id
                  ? <Button size="sm" kind="danger" onClick={() => onDelete(d)}>Confirm</Button>
                  : <Button size="sm" kind="ghost" aria-label={`Delete ${d.filename}`} onClick={() => setConfirm(d.id)}><Icon name="trash" /></Button>)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function WatchedFolder({ client, data, isSuper, onChanged, onScan, scanning }: { client: TabProps["client"]; data: DocsData; isSuper: boolean; onChanged: (job: Job | null) => void; onScan: () => void; scanning: boolean }) {
  const [path, setPath] = useState(data.settings.watch_path ?? "");
  const [interval, setInterval] = useState(data.settings.scan_interval_minutes);
  const [error, setError] = useState("");
  const [browsing, setBrowsing] = useState(false);
  const { busy, run } = useAction();
  const save = () => run(async () => {
    setError("");
    try {
      const res = await api<{ job: Job | null }>(`/api/admin/clients/${client.client_id}/documents/settings`, { method: "PUT", body: { watch_path: path, scan_interval_minutes: interval } });
      onChanged(res.job);
    } catch (e) {
      setError((e as Error).message);
    }
  });
  return (
    <Card
      title="Watched folder"
      subtitle={<>Index a folder on the server automatically instead of uploading files: new and changed files are indexed on every scan, deleted ones are removed. Put the files in the server's watched folder (<code>WATCHED_HOST_DIR</code> in <code>.env</code>, by default <code>watched</code> in the app's folder), then choose the client's sub-folder here.</>}
      actions={data.settings.watch_path && <Button icon="refresh" disabled={scanning} onClick={onScan}>Scan now</Button>}
    >
      <div className="stack">
        {error && <Alert kind="error">{error}</Alert>}
        {isSuper ? (
          <div className="form-grid">
            <Field label="Folder" htmlFor="wf-path" hint="Choose with Browse, or type a path. Leave empty to disconnect.">
              <div className="row" style={{ flexWrap: "nowrap" }}>
                <input id="wf-path" className="input mono" value={path} onChange={(e) => setPath(e.target.value)} placeholder={`${data.watched_root}/${client.client_id}`} />
                <Button icon="folder" onClick={() => setBrowsing(true)}>Browse…</Button>
              </div>
            </Field>
            <Field label="Scan every (minutes)" htmlFor="wf-int"><input id="wf-int" className="input" type="number" min={5} max={10080} value={interval} onChange={(e) => setInterval(Number(e.target.value))} /></Field>
          </div>
        ) : (
          <dl className="kv"><dt>Folder</dt><dd className="mono">{data.settings.watch_path}</dd></dl>
        )}
        <div className="row between">
          <span className="small muted">Last scan: {relative(data.settings.last_scan_at)}</span>
          {isSuper && <Button kind="primary" loading={busy} onClick={save}>Save folder</Button>}
        </div>
      </div>
      {browsing && <FolderPicker clientId={client.client_id} start={path} onClose={() => setBrowsing(false)} onPick={(p) => { setPath(p); setBrowsing(false); }} />}
    </Card>
  );
}

interface FolderList {
  root: string; path: string; parent: string | null; files: number; host_dir: string;
  folders: { name: string; path: string; files: number; folders: number }[];
}

/** Browse the server's watched folder (not this computer's): the files have to be on the server. */
function FolderPicker({ clientId, start, onClose, onPick }: { clientId: string; start: string; onClose: () => void; onPick: (path: string) => void }) {
  const [list, setList] = useState<FolderList | null>(null);
  const [error, setError] = useState("");
  const open = useCallback((path: string | null) => {
    setError("");
    api<FolderList>(`/api/admin/clients/${clientId}/documents/folders${path ? `?path=${encodeURIComponent(path)}` : ""}`)
      .then(setList)
      .catch((e) => {
        if (path) open(null); // the typed path doesn't exist: start at the top instead
        else setError((e as Error).message);
      });
  }, [clientId]);
  useEffect(() => {
    open(start.trim() || null);
  }, [open, start]);

  const relativeName = list ? (list.path === list.root ? "(top level)" : list.path.slice(list.root.length + 1)) : "";
  return (
    <Modal
      title="Choose a folder on the server"
      onClose={onClose}
      wide
      footer={<><Button onClick={onClose}>Cancel</Button><span className="spacer" /><Button kind="primary" disabled={!list} onClick={() => list && onPick(list.path)}>Use this folder</Button></>}
    >
      <div className="stack">
        {error && <Alert kind="error">{error}</Alert>}
        {list && (
          <>
            <p className="muted small" style={{ margin: 0 }}>
              These are folders on the server, inside its watched folder (<code>{list.host_dir}</code> on the host, <code>{list.root}</code> in the app).
              To add one, create it there (for example <code>{list.host_dir}/{clientId}</code>) and copy the documents into it.
            </p>
            <div className="row between">
              <span><strong className="mono">{relativeName}</strong> <span className="small muted">· {list.files} document{list.files === 1 ? "" : "s"} here</span></span>
              {list.parent && <Button size="sm" onClick={() => open(list.parent)}>↑ Up</Button>}
            </div>
            <div className="table-wrap" style={{ maxHeight: 340, overflowY: "auto" }}>
              <table className="table">
                <tbody>
                  {list.folders.map((f) => (
                    <tr key={f.path} style={{ cursor: "pointer" }} onClick={() => open(f.path)}>
                      <td>📁 <strong>{f.name}</strong></td>
                      <td className="small muted">{f.files} document{f.files === 1 ? "" : "s"}{f.folders ? ` · ${f.folders} folder${f.folders === 1 ? "" : "s"}` : ""}</td>
                      <td className="num"><Button size="sm" onClick={(e) => { e.stopPropagation(); onPick(f.path); }}>Choose</Button></td>
                    </tr>
                  ))}
                  {list.folders.length === 0 && <tr><td className="muted">No sub-folders here.</td></tr>}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>
    </Modal>
  );
}
