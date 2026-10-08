import { useEffect, useState } from "react";
import { api, type AIModel } from "../api";
import { PageHead } from "../components/Layout";
import { Alert, Badge, Button, Card, Loading, Progress, useAction } from "../components/ui";
import { bytes, dateTime, duration, relative } from "../format";

interface SystemData {
  version: string;
  mode: string;
  summary: { database: boolean; worker: boolean; worker_age_seconds: number | null; scheduler: boolean; scheduler_age_seconds: number | null; embedding: string; embedding_model: string };
  database: { version?: string; size_bytes?: number; pgvector?: string };
  embedding: { model: string; dim: number | null; status: string; error: string | null };
  models: AIModel[];
  disk: { path: string; total: number; used: number; free: number }[];
}

export function System() {
  const [data, setData] = useState<SystemData | null>(null);
  const [checks, setChecks] = useState<Record<number, { ok: boolean; response_ms?: number; error?: string }>>({});
  const { busy, run } = useAction();
  const load = () => api<SystemData>("/api/admin/system").then(setData);
  useEffect(() => {
    load();
  }, []);
  if (!data) return <div className="page"><Loading /></div>;
  const s = data.summary;
  const ago = (sec: number | null) => (sec === null ? "never" : sec < 60 ? `${Math.round(sec)} s ago` : `${Math.round(sec / 60)} min ago`);

  const checkAll = () =>
    run(async () => {
      for (const m of data.models) {
        const r = await api<{ ok: boolean; response_ms?: number; error?: string }>(`/api/admin/system/models/${m.id}/check`, { method: "POST" });
        setChecks((c) => ({ ...c, [m.id]: r }));
      }
      await load();
    });

  return (
    <div className="page stack">
      <PageHead
        title="System health"
        description={`Version ${data.version} · ${data.mode === "onprem" ? "On-premise" : "Cloud"} mode`}
        actions={<a className="btn" href="/api/admin/system/backup" download>Download backup</a>}
      />
      <div className="grid grid-4">
        <Status label="Database" ok={s.database} detail={`PostgreSQL ${data.database.version ?? "?"} · pgvector ${data.database.pgvector ?? "?"}`} />
        <Status label="Worker" ok={s.worker} detail={`Heartbeat ${ago(s.worker_age_seconds)}`} />
        <Status label="Scheduler" ok={s.scheduler} detail={`Heartbeat ${ago(s.scheduler_age_seconds)}`} />
        <Status label="Embedding model" ok={data.embedding.status === "ready"} detail={`${data.embedding.model} · ${data.embedding.status}${data.embedding.dim ? ` · ${data.embedding.dim} dims` : ""}`} />
      </div>
      {!s.worker && <Alert kind="error">The worker is not sending heartbeats, so crawls and indexing won't run. Check <code>docker compose logs worker</code>.</Alert>}

      <Card title="AI models" subtitle="Checks send a small test prompt to each model (cloud models may charge a tiny amount)." actions={<Button icon="play" loading={busy} onClick={checkAll}>Check all models</Button>} bodyless>
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>Model</th><th>Provider</th><th>Role</th><th>Last check</th><th>Result</th></tr></thead>
            <tbody>
              {data.models.map((m) => {
                const live = checks[m.id];
                const ok = live ? live.ok : m.last_test_ok;
                return (
                  <tr key={m.id}>
                    <td><strong>{m.name}</strong><div className="small muted mono">{m.model_name}</div></td>
                    <td className="small">{m.provider_label}</td>
                    <td>{m.is_default && <Badge color="teal">Default</Badge>} {m.is_fallback && <Badge color="blue">Fallback</Badge>}</td>
                    <td className="small muted">{relative(m.last_test_at)}</td>
                    <td className="small">
                      {ok === null ? <span className="muted">Not tested</span> : ok ? <Badge color="green"><span className="dot" />OK{live?.response_ms ? ` · ${duration(live.response_ms)}` : ""}</Badge> : <><Badge color="red"><span className="dot" />Failed</Badge><div className="small muted">{live?.error ?? m.last_test_message}</div></>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Card>

      <div className="grid grid-2">
        <Card title="Disk usage">
          <div className="stack">
            {data.disk.map((d) => (
              <div key={d.path}>
                <div className="row between small"><span className="mono">{d.path}</span><span className="muted">{bytes(d.used)} of {bytes(d.total)} · {bytes(d.free)} free</span></div>
                <Progress value={(d.used / d.total) * 100} />
              </div>
            ))}
            <div className="small muted">Database size: {bytes(data.database.size_bytes ?? 0)}</div>
          </div>
        </Card>
        <Card title="Backups">
          <p className="muted">The backup is a PostgreSQL dump of everything: settings, users, clients, content and questions. Restore it with <code>pg_restore</code>.</p>
          <Alert kind="warning">Saved API keys in the backup are encrypted with this server's <code>SECRET_KEY</code>. Keep a copy of <code>.env</code> with the backup. Uploaded files and logos live in the <code>appdata</code> Docker volume and are not included.</Alert>
          <p className="small muted" style={{ marginTop: 10 }}>Server time: {dateTime(new Date().toISOString())}</p>
        </Card>
      </div>
    </div>
  );
}

function Status({ label, ok, detail }: { label: string; ok: boolean; detail: string }) {
  return (
    <div className="card stat">
      <div className="row between"><span className="stat-label">{label}</span><Badge color={ok ? "green" : "red"}><span className="dot" />{ok ? "OK" : "Problem"}</Badge></div>
      <div className="small muted" style={{ marginTop: 8 }}>{detail}</div>
    </div>
  );
}
