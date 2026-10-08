// Live progress for a background job (polls until it finishes).
import { useEffect, useRef, useState } from "react";
import { api, type Job } from "../api";
import { Button, Progress, StatusBadge } from "./ui";

export function useJob(jobId: number | null | undefined, onDone?: (job: Job) => void) {
  const [job, setJob] = useState<Job | null>(null);
  const doneRef = useRef(onDone);
  doneRef.current = onDone;
  useEffect(() => {
    if (!jobId) return;
    let stop = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const { job: j } = await api<{ job: Job }>(`/api/admin/jobs/${jobId}`);
        if (stop) return;
        setJob(j);
        if (j.status === "queued" || j.status === "running") timer = window.setTimeout(poll, 1500);
        else doneRef.current?.(j);
      } catch {
        if (!stop) timer = window.setTimeout(poll, 4000);
      }
    };
    poll();
    return () => {
      stop = true;
      window.clearTimeout(timer);
    };
  }, [jobId]);
  return job;
}

export function JobProgress({ jobId, onDone, showLog }: { jobId: number; onDone?: (job: Job) => void; showLog?: boolean }) {
  const job = useJob(jobId, onDone);
  if (!job) return <Progress value={0} />;
  const active = job.status === "queued" || job.status === "running";
  return (
    <div className="stack" style={{ gap: 8 }}>
      <div className="row between">
        <div className="row"><strong>{job.type_label}</strong><StatusBadge status={job.status} /></div>
        {active && (
          <Button size="sm" onClick={() => api(`/api/admin/jobs/${job.id}/cancel`, { method: "POST" })} disabled={job.cancel_requested}>
            {job.cancel_requested ? "Cancelling…" : "Cancel"}
          </Button>
        )}
      </div>
      {active && <Progress value={job.status === "queued" ? 0 : job.progress} />}
      <div className="small muted truncate">{job.status === "queued" ? "Waiting for the worker…" : job.error || job.progress_text}</div>
      {showLog && job.log && <div className="log-box">{job.log}</div>}
    </div>
  );
}
