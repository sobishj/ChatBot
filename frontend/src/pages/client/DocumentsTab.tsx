import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, type Job } from "../../api";
import { JobProgress } from "../../components/JobProgress";
import { Alert, Badge, Button, Card, Empty, Field, Loading, Modal, StatusBadge, useAction, useToast } from "../../components/ui";
import { Icon } from "../../components/icons";
import { approxTime, bytes, money, num, relative } from "../../format";
import type { TabProps } from "./ClientDetail";

interface Doc { id: number; source: "upload" | "folder"; path: string; filename: string; size_bytes: number; status: string; error: string | null; chunk_count: number; uploaded_at: string; indexed_at: string | null }
interface DocsData { documents: Doc[]; settings: { watch_path: string | null; scan_interval_minutes: number; last_scan_at: string | null; answer_any_topic: boolean }; watched_root: string; mode: string }

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
          <DocTable docs={[...uploads, ...folder]} onDelete={(d) => run(async () => { await api(`/api/admin/clients/${client.client_id}/documents/${d.id}`, { method: "DELETE" }); await load(); reload(); }, "Document deleted")} />
        )}
      </Card>

      <Card title="Answering from documents">
        <div className="stack">
          <label className="check">
            <input type="checkbox" checked={data.settings.answer_any_topic} disabled={busy}
              onChange={(e) => run(async () => {
                const r = await api<{ settings: DocsData["settings"] }>(`/api/admin/clients/${client.client_id}/documents/answering`, { method: "PUT", body: { answer_any_topic: e.target.checked } });
                setData({ ...data, settings: r.settings });
              }, e.target.checked ? "The assistant now answers anything the documents cover" : "The assistant now keeps to this client's business")} />
            Answer any question the uploaded documents cover, even if it's unrelated to {client.name}
          </label>
          <p className="hint" style={{ margin: 0 }}>
            Off (default): the assistant only answers questions about {client.name} and politely declines others, even when a document
            mentions them. On: if an uploaded document answers the question (for example a letter or manual on another subject), it answers.
            Answers always come from this client's own content, never from general knowledge.
          </p>
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

function DocTable({ docs, onDelete }: { docs: Doc[]; onDelete: (d: Doc) => void }) {
  const [confirm, setConfirm] = useState<number | null>(null);
  return (
    <div className="table-wrap">
      <table className="table">
        <thead><tr><th>File</th><th>Source</th><th className="num">Size</th><th>Status</th><th>Indexed</th><th /></tr></thead>
        <tbody>
          {docs.map((d) => (
            <tr key={d.id}>
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
      subtitle={<>The worker scans a server folder on a schedule and indexes new or changed files. Folders must be inside <code>{data.watched_root}</code>, which is mounted from the host (<code>WATCHED_HOST_DIR</code> in <code>.env</code>). Mount network shares on the host first.</>}
      actions={data.settings.watch_path && <Button icon="refresh" disabled={scanning} onClick={onScan}>Scan now</Button>}
    >
      <div className="stack">
        {error && <Alert kind="error">{error}</Alert>}
        {isSuper ? (
          <div className="form-grid">
            <Field label="Folder path" htmlFor="wf-path" hint={`e.g. ${data.watched_root}/${client.client_id}. Leave empty to disconnect.`}><input id="wf-path" className="input mono" value={path} onChange={(e) => setPath(e.target.value)} placeholder={`${data.watched_root}/${client.client_id}`} /></Field>
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
    </Card>
  );
}
