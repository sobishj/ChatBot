import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, type Job } from "../api";
import { useAuth } from "../auth";
import { DailyChart } from "../components/BarChart";
import { PageHead } from "../components/Layout";
import { Badge, Card, Empty, Loading, Stat, StatusBadge } from "../components/ui";
import { money, num, pct, relative } from "../format";

interface DashboardData {
  clients: number;
  active_clients: number;
  questions_today: number;
  questions_30d: number;
  unanswered_rate_30d: number;
  tokens_30d: { input: number; output: number; cost: number };
  per_day: { date: string; total: number; unanswered: number }[];
  top_clients: { client_id: string; name: string; questions: number }[];
  recent_jobs: Job[];
  health: null | { database: boolean; worker: boolean; scheduler: boolean; embedding: string; models_total: number; models_failing: number; default_model_set: boolean };
}

export function Dashboard() {
  const { mode } = useAuth();
  const [data, setData] = useState<DashboardData | null>(null);
  useEffect(() => {
    api<DashboardData>("/api/admin/dashboard").then(setData);
  }, []);
  if (!data) return <div className="page"><Loading /></div>;
  const h = data.health;
  return (
    <div className="page stack">
      <PageHead title="Dashboard" description="Last 30 days across your assistants." />
      <div className="grid grid-4">
        <Stat label={mode === "onprem" ? "Assistant" : "Clients"} value={num(data.clients)} sub={`${num(data.active_clients)} active`} />
        <Stat label="Questions today" value={num(data.questions_today)} sub={`${num(data.questions_30d)} in 30 days`} />
        <Stat label="Unanswered rate" value={pct(data.unanswered_rate_30d)} sub="Questions the assistant couldn't answer" />
        <Stat label="Estimated AI cost" value={money(data.tokens_30d.cost)} sub={`${num(data.tokens_30d.input + data.tokens_30d.output)} tokens`} />
      </div>
      <Card><DailyChart data={data.per_day} /></Card>
      <div className="grid grid-2">
        <Card title="Recent jobs" actions={<Link to="/jobs">All jobs</Link>} bodyless>
          {data.recent_jobs.length === 0 ? <Empty title="No jobs yet">Crawls and indexing runs appear here.</Empty> : (
            <table className="table">
              <tbody>
                {data.recent_jobs.map((j) => (
                  <tr key={j.id}>
                    <td><strong>{j.type_label}</strong><div className="small muted">{j.client_name ?? "System"}</div></td>
                    <td><StatusBadge status={j.status} /></td>
                    <td className="small muted nowrap">{relative(j.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
        {h ? (
          <Card title="System health" actions={<Link to="/system">Details</Link>}>
            <div className="stack" style={{ gap: 10 }}>
              <HealthRow label="Database" ok={h.database} />
              <HealthRow label="Worker" ok={h.worker} detail={h.worker ? "running" : "not responding"} />
              <HealthRow label="Scheduler" ok={h.scheduler} detail={h.scheduler ? "running" : "not responding"} />
              <HealthRow label="Embedding model" ok={h.embedding === "ready"} detail={h.embedding} />
              <HealthRow label="AI models" ok={h.default_model_set && h.models_failing === 0} detail={`${h.models_total} saved${h.models_failing ? `, ${h.models_failing} failing last test` : ""}`} />
            </div>
          </Card>
        ) : (
          <Card title="Most active">
            {data.top_clients.map((c) => (
              <div key={c.client_id} className="row between" style={{ padding: "6px 0" }}>
                <Link to={`/clients/${c.client_id}`}>{c.name}</Link><span className="muted">{num(c.questions)} questions</span>
              </div>
            ))}
          </Card>
        )}
      </div>
    </div>
  );
}

function HealthRow({ label, ok, detail }: { label: string; ok: boolean; detail?: string }) {
  return (
    <div className="row between">
      <span>{label}</span>
      <Badge color={ok ? "green" : "red"}><span className="dot" />{ok ? "OK" : "Problem"}{detail ? ` · ${detail}` : ""}</Badge>
    </div>
  );
}
