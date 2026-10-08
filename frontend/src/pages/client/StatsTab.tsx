import { useEffect, useState } from "react";
import { api, type Stats } from "../../api";
import { DailyChart, ShareBars } from "../../components/BarChart";
import { Card, Empty, Loading, Segmented, Stat } from "../../components/ui";
import { dateTime, duration, money, num, pct } from "../../format";
import type { TabProps } from "./ClientDetail";

const LANGUAGE_NAMES: Record<string, string> = { en: "English", ml: "Malayalam", hi: "Hindi", ta: "Tamil", ar: "Arabic", kn: "Kannada", te: "Telugu", fr: "French", de: "German", es: "Spanish", unknown: "Unknown" };

export function StatsTab({ client }: TabProps) {
  const [days, setDays] = useState(30);
  const [stats, setStats] = useState<Stats | null>(null);
  useEffect(() => {
    setStats(null);
    api<Stats>(`/api/admin/clients/${client.client_id}/stats?days=${days}`).then(setStats);
  }, [client.client_id, days]);

  return (
    <div className="stack">
      <div className="row between">
        <span className="muted">Visitor questions from the website widget (test chats excluded).</span>
        <Segmented options={[{ value: 7, label: "7 days" }, { value: 30, label: "30 days" }, { value: 90, label: "90 days" }]} value={days} onChange={setDays} />
      </div>
      {!stats ? <Loading /> : (
        <>
          <div className="grid grid-4">
            <Stat label="Questions" value={num(stats.total)} sub={`Avg response ${duration(stats.avg_response_ms)}`} />
            <Stat label="Answered" value={num(stats.answered)} />
            <Stat label="Unanswered" value={num(stats.unanswered)} sub={`${pct(stats.unanswered_rate)} of questions`} />
            <Stat label="AI usage" value={money(stats.tokens.cost)} sub={`${num(stats.tokens.input)} in / ${num(stats.tokens.output)} out tokens`} />
          </div>
          <Card><DailyChart data={stats.per_day} /></Card>
          <div className="grid grid-2">
            <Card title="Top questions" subtitle="Grouped by normalised text." bodyless>
              {stats.top_questions.length === 0 ? <Empty title="No questions yet" /> : (
                <table className="table">
                  <thead><tr><th>Question</th><th className="num">Asked</th><th className="num">Unanswered</th></tr></thead>
                  <tbody>{stats.top_questions.map((q) => <tr key={q.normalized}><td>{q.question}</td><td className="num">{num(q.count)}</td><td className="num">{q.unanswered ? <span style={{ color: "var(--warning)" }}>{num(q.unanswered)}</span> : "0"}</td></tr>)}</tbody>
                </table>
              )}
            </Card>
            <Card title="Unanswered questions" subtitle="Add this information to the website or upload a document." bodyless>
              {stats.unanswered_questions.length === 0 ? <Empty title="Nothing unanswered" /> : (
                <div style={{ maxHeight: 460, overflow: "auto" }}>
                  <table className="table">
                    <tbody>{stats.unanswered_questions.map((q) => <tr key={q.id}><td>{q.question}<div className="small muted">{dateTime(q.created_at)} · score {(q.top_score ?? 0).toFixed(2)}</div></td></tr>)}</tbody>
                  </table>
                </div>
              )}
            </Card>
          </div>
          <div className="grid grid-2">
            <Card title="Languages">
              {stats.languages.length === 0 ? <Empty title="No data" /> : <ShareBars items={stats.languages.map((l) => ({ label: LANGUAGE_NAMES[l.language] ?? l.language, value: l.count }))} />}
            </Card>
            <Card title="Token usage by model" bodyless>
              {stats.models.length === 0 ? <Empty title="No data" /> : (
                <table className="table">
                  <thead><tr><th>Model</th><th className="num">Answers</th><th className="num">Tokens</th><th className="num">Cost</th></tr></thead>
                  <tbody>{stats.models.map((m) => <tr key={m.model}><td>{m.model}{m.fallbacks > 0 && <div className="small muted">{m.fallbacks} via fallback</div>}</td><td className="num">{num(m.count)}</td><td className="num">{num(m.input_tokens + m.output_tokens)}</td><td className="num">{money(m.cost)}</td></tr>)}</tbody>
                </table>
              )}
            </Card>
          </div>
        </>
      )}
    </div>
  );
}
