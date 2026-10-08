import { useCallback, useEffect, useState } from "react";
import { api } from "../../api";
import { Badge, Button, Card, Empty, Loading, Modal } from "../../components/ui";
import { dateTime, duration, num } from "../../format";
import type { TabProps } from "./ClientDetail";

interface Row {
  id: number;
  session_id: string;
  channel: string;
  question: string;
  answer: string;
  sources: { title: string; url?: string }[];
  answered: boolean;
  top_score: number | null;
  language: string | null;
  model_name: string | null;
  used_fallback: boolean;
  response_ms: number;
  error: string | null;
  created_at: string;
}

export function ConversationsTab({ client }: TabProps) {
  const [search, setSearch] = useState("");
  const [answered, setAnswered] = useState("all");
  const [channel, setChannel] = useState("widget");
  const [page, setPage] = useState(1);
  const [data, setData] = useState<{ total: number; items: Row[] } | null>(null);
  const [open, setOpen] = useState<Row | null>(null);

  const query = `search=${encodeURIComponent(search)}&answered=${answered}&channel=${channel}`;
  const load = useCallback(() => api<{ total: number; items: Row[] }>(`/api/admin/clients/${client.client_id}/conversations?${query}&page=${page}`).then(setData), [client.client_id, query, page]);
  useEffect(() => {
    const t = window.setTimeout(load, 250);
    return () => window.clearTimeout(t);
  }, [load]);

  return (
    <Card
      title={`Conversations${data ? ` (${num(data.total)})` : ""}`}
      subtitle="Phone numbers and email addresses are masked before saving."
      actions={
        <>
          <input className="input" style={{ width: 220 }} placeholder="Search…" value={search} onChange={(e) => { setSearch(e.target.value); setPage(1); }} aria-label="Search conversations" />
          <select className="select" style={{ width: 150 }} value={answered} onChange={(e) => { setAnswered(e.target.value); setPage(1); }} aria-label="Answered filter">
            <option value="all">All answers</option><option value="yes">Answered</option><option value="no">Unanswered</option>
          </select>
          <select className="select" style={{ width: 140 }} value={channel} onChange={(e) => { setChannel(e.target.value); setPage(1); }} aria-label="Channel">
            <option value="widget">Website</option><option value="test">Test chat</option><option value="cli">CLI</option><option value="all">All channels</option>
          </select>
          <a className="btn" href={`/api/admin/clients/${client.client_id}/conversations.csv?${query}`} download>Export CSV</a>
        </>
      }
      bodyless
    >
      {!data ? <Loading /> : data.items.length === 0 ? <Empty title="No conversations found" /> : (
        <>
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>When</th><th>Question</th><th>Answer</th><th>Status</th></tr></thead>
              <tbody>
                {data.items.map((r) => (
                  <tr key={r.id} className="clickable" onClick={() => setOpen(r)}>
                    <td className="small muted nowrap">{dateTime(r.created_at)}</td>
                    <td style={{ maxWidth: 300 }}>{r.question}</td>
                    <td style={{ maxWidth: 380 }} className="small"><span style={{ display: "-webkit-box", WebkitLineClamp: 3, WebkitBoxOrient: "vertical", overflow: "hidden" }}>{r.answer}</span></td>
                    <td>{r.error ? <Badge color="red">Error</Badge> : r.answered ? <Badge color="green">Answered</Badge> : <Badge color="amber">Unanswered</Badge>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {data.total > 50 && (
            <div className="card-body row between">
              <Button size="sm" disabled={page === 1} onClick={() => setPage(page - 1)}>Previous</Button>
              <span className="small muted">Page {page} of {Math.ceil(data.total / 50)}</span>
              <Button size="sm" disabled={page * 50 >= data.total} onClick={() => setPage(page + 1)}>Next</Button>
            </div>
          )}
        </>
      )}
      {open && (
        <Modal title="Conversation entry" onClose={() => setOpen(null)} wide>
          <div className="stack">
            <div><span className="label">Question</span><p>{open.question}</p></div>
            <div><span className="label">Answer</span><p style={{ whiteSpace: "pre-wrap" }}>{open.answer}</p></div>
            {open.sources.length > 0 && <div><span className="label">Sources</span><ul>{open.sources.map((s, i) => <li key={i}>{s.url ? <a href={s.url} target="_blank" rel="noreferrer">{s.title}</a> : s.title}</li>)}</ul></div>}
            <dl className="kv">
              <dt>Time</dt><dd>{dateTime(open.created_at)}</dd>
              <dt>Session</dt><dd className="mono small">{open.session_id}</dd>
              <dt>Channel</dt><dd>{open.channel}</dd>
              <dt>Language</dt><dd>{open.language ?? "—"}</dd>
              <dt>Confidence</dt><dd>{(open.top_score ?? 0).toFixed(3)}</dd>
              <dt>Model</dt><dd>{open.model_name ?? "—"}{open.used_fallback ? " (fallback)" : ""} · {duration(open.response_ms)}</dd>
              {open.error && (<><dt>Error</dt><dd style={{ color: "var(--danger)" }}>{open.error}</dd></>)}
            </dl>
          </div>
        </Modal>
      )}
    </Card>
  );
}
