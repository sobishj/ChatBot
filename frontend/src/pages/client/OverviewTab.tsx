import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../../api";
import { JobProgress } from "../../components/JobProgress";
import { Alert, Button, Card, Stat, StatusBadge, useAction } from "../../components/ui";
import { dateTime, num, relative } from "../../format";
import type { TabProps } from "./ClientDetail";

export function OverviewTab({ client, reload, isSuper }: TabProps) {
  const navigate = useNavigate();
  const { busy, run } = useAction();
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [name, setName] = useState(client.name);
  const [website, setWebsite] = useState(client.website_url ?? "");
  const nothingIndexed = client.chunks === 0;

  return (
    <div className="stack">
      {nothingIndexed && (
        <Alert kind="info">
          No content indexed yet. Go to <strong>Website</strong> and press <strong>Crawl now</strong>, or upload files under <strong>Documents</strong>.
        </Alert>
      )}
      <div className="grid grid-4">
        <Stat label="Web pages" value={num(client.pages)} sub={`Last crawl ${relative(client.last_crawl_at)}`} />
        <Stat label="Documents" value={num(client.documents)} />
        <Stat label="Searchable chunks" value={num(client.chunks)} />
        <Stat label="Questions (30 days)" value={num(client.questions_30d)} />
      </div>

      {client.active_jobs && client.active_jobs.length > 0 && (
        <Card title="Running now">
          <div className="stack">{client.active_jobs.map((j) => <JobProgress key={j.id} jobId={j.id} onDone={() => reload()} />)}</div>
        </Card>
      )}

      <div className="grid grid-2">
        <Card title="Details">
          {isSuper ? (
            <div className="stack">
              <div className="field"><label htmlFor="o-name">Name</label><input id="o-name" className="input" value={name} onChange={(e) => setName(e.target.value)} /></div>
              <div className="field"><label htmlFor="o-web">Website URL</label><input id="o-web" className="input" value={website} onChange={(e) => setWebsite(e.target.value)} /></div>
              <div className="row between">
                <Button
                  onClick={() => run(async () => { await api(`/api/admin/clients/${client.client_id}`, { method: "PATCH", body: { active: !client.active } }); await reload(); }, client.active ? "Client deactivated" : "Client activated")}
                  loading={busy}
                >
                  {client.active ? "Deactivate" : "Activate"}
                </Button>
                <Button kind="primary" loading={busy} onClick={() => run(async () => { await api(`/api/admin/clients/${client.client_id}`, { method: "PATCH", body: { name, website_url: website } }); await reload(); }, "Saved")}>Save</Button>
              </div>
              <p className="hint">Inactive clients' widgets stay hidden and their content isn't re-crawled.</p>
            </div>
          ) : (
            <dl className="kv">
              <dt>Client ID</dt><dd className="mono">{client.client_id}</dd>
              <dt>Website</dt><dd>{client.website_url ?? "—"}</dd>
              <dt>Created</dt><dd>{dateTime(client.created_at)}</dd>
            </dl>
          )}
        </Card>
        <Card title="Recent jobs" bodyless>
          <table className="table">
            <tbody>
              {(client.recent_jobs ?? []).map((j) => (
                <tr key={j.id}><td>{j.type_label}</td><td><StatusBadge status={j.status} /></td><td className="small muted">{relative(j.created_at)}</td></tr>
              ))}
              {(client.recent_jobs ?? []).length === 0 && <tr><td className="muted">No jobs yet.</td></tr>}
            </tbody>
          </table>
        </Card>
      </div>

      {isSuper && client.mode !== "onprem" && (
        <Card title="Danger zone">
          <div className="row between">
            <span className="muted">Delete this client with all its pages, documents, uploaded files and questions. This cannot be undone.</span>
            {confirmDelete ? (
              <Button kind="danger" loading={busy} onClick={() => run(async () => { await api(`/api/admin/clients/${client.client_id}`, { method: "DELETE" }); navigate("/clients"); }, "Client deleted")}>Yes, delete {client.client_id}</Button>
            ) : (
              <Button kind="danger" icon="trash" onClick={() => setConfirmDelete(true)}>Delete client</Button>
            )}
          </div>
        </Card>
      )}
    </div>
  );
}
