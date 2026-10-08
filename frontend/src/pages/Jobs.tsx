import { Fragment, useCallback, useEffect, useState } from "react";
import { api, type Job } from "../api";
import { PageHead } from "../components/Layout";
import { Button, Card, Empty, Loading, Modal, Progress, StatusBadge, useAction } from "../components/ui";
import { dateTime, relative } from "../format";

export function Jobs() {
  const [jobs, setJobs] = useState<Job[] | null>(null);
  const [status, setStatus] = useState("");
  const [open, setOpen] = useState<number | null>(null);

  const load = useCallback(
    () => api<{ jobs: Job[] }>(`/api/admin/jobs${status ? `?status=${status}` : ""}`).then((d) => setJobs(d.jobs)),
    [status],
  );
  useEffect(() => {
    load();
  }, [load]);
  // Auto-refresh while anything is queued or running.
  useEffect(() => {
    if (!jobs?.some((j) => j.status === "queued" || j.status === "running")) return;
    const t = window.setTimeout(load, 2000);
    return () => window.clearTimeout(t);
  }, [jobs, load]);

  return (
    <div className="page stack">
      <PageHead
        title="Jobs"
        description="Crawls, document indexing and other background work."
        actions={
          <select className="select" style={{ width: 180 }} value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Filter by status">
            <option value="">All statuses</option>
            {["queued", "running", "succeeded", "failed", "cancelled"].map((s) => <option key={s} value={s}>{s[0].toUpperCase() + s.slice(1)}</option>)}
          </select>
        }
      />
      <Card bodyless>
        {!jobs ? <Loading /> : jobs.length === 0 ? <Empty title="No jobs" /> : (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Job</th><th>Client</th><th>Status</th><th style={{ width: 220 }}>Progress</th><th>Started</th><th>Trigger</th></tr></thead>
              <tbody>
                {jobs.map((j) => (
                  <tr key={j.id} className="clickable" onClick={() => setOpen(j.id)}>
                    <td><strong>{j.type_label}</strong><div className="small muted">#{j.id}</div></td>
                    <td>{j.client_name ?? <span className="muted">System</span>}</td>
                    <td><StatusBadge status={j.status} /></td>
                    <td>
                      {j.status === "running" ? <><Progress value={j.progress} /><div className="small muted truncate" style={{ maxWidth: 220 }}>{j.progress_text}</div></> : <span className="small muted truncate" style={{ maxWidth: 220 }}>{j.error ?? j.progress_text ?? ""}</span>}
                    </td>
                    <td className="small muted nowrap">{relative(j.started_at ?? j.created_at)}</td>
                    <td className="small muted">{j.triggered_by}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      {open !== null && <JobDetail id={open} onClose={() => setOpen(null)} onChanged={load} />}
    </div>
  );
}

export function JobDetail({ id, onClose, onChanged }: { id: number; onClose: () => void; onChanged: () => void }) {
  const [job, setJob] = useState<Job | null>(null);
  const { busy, run } = useAction();
  const load = useCallback(() => api<{ job: Job }>(`/api/admin/jobs/${id}`).then((d) => setJob(d.job)), [id]);
  useEffect(() => {
    load();
  }, [load]);
  useEffect(() => {
    if (job && (job.status === "running" || job.status === "queued")) {
      const t = window.setTimeout(load, 1500);
      return () => window.clearTimeout(t);
    }
  }, [job, load]);
  if (!job) return null;
  const active = job.status === "running" || job.status === "queued";
  return (
    <Modal
      title={`${job.type_label} #${job.id}`}
      onClose={onClose}
      wide
      footer={
        <>
          {active && <Button kind="danger" loading={busy} disabled={job.cancel_requested} onClick={() => run(async () => { await api(`/api/admin/jobs/${job.id}/cancel`, { method: "POST" }); await load(); onChanged(); })}>{job.cancel_requested ? "Cancelling…" : "Cancel job"}</Button>}
          {!active && <Button kind="primary" icon="refresh" loading={busy} onClick={() => run(async () => { await api(`/api/admin/jobs/${job.id}/retry`, { method: "POST" }); onChanged(); onClose(); }, "Job queued again")}>Retry</Button>}
          <Button onClick={onClose}>Close</Button>
        </>
      }
    >
      <div className="stack">
        <dl className="kv">
          <dt>Status</dt><dd><StatusBadge status={job.status} /></dd>
          <dt>Client</dt><dd>{job.client_name ?? "System"}</dd>
          <dt>Created</dt><dd>{dateTime(job.created_at)} · {job.triggered_by}</dd>
          <dt>Started / finished</dt><dd>{dateTime(job.started_at)} → {dateTime(job.finished_at)}</dd>
          {job.error && (<><dt>Error</dt><dd style={{ color: "var(--danger)" }}>{job.error}</dd></>)}
        </dl>
        {active && <><Progress value={job.progress} /><div className="small muted">{job.progress_text}</div></>}
        {job.result && (
          <div>
            <h3 style={{ marginBottom: 6 }}>Result</h3>
            <dl className="kv">
              {Object.entries(job.result).filter(([, v]) => typeof v !== "object" || v === null).map(([k, v]) => (<Fragment key={k}><dt>{k.replace(/_/g, " ")}</dt><dd>{String(v)}</dd></Fragment>))}
            </dl>
          </div>
        )}
        <div>
          <h3 style={{ marginBottom: 6 }}>Log</h3>
          <div className="log-box">{job.log || "No log output yet."}</div>
        </div>
      </div>
    </Modal>
  );
}
