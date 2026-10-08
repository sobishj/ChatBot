// Chat exactly as a visitor would, with diagnostics (confidence, model, timing, retrieved chunks).
import { useEffect, useRef, useState, type FormEvent } from "react";
import { api } from "../../api";
import { Badge, Button, Card, Empty } from "../../components/ui";
import { duration, money } from "../../format";
import type { TabProps } from "./ClientDetail";

interface Answer {
  answer: string;
  sources: { type: "web" | "doc"; title: string; url?: string }[];
  answered: boolean;
  confidence: number;
  threshold: number;
  model_name: string | null;
  used_fallback: boolean;
  response_ms: number;
  input_tokens: number;
  output_tokens: number;
  cost: number;
  language: string;
  error: string | null;
  chunks: { title: string; source: string; source_type: string; similarity: number | null; keyword: boolean; preview: string }[];
}

type Turn = { question: string; answer?: Answer; failed?: string };

const newSession = () => "test-" + Math.random().toString(36).slice(2, 12) + Date.now().toString(36);

export function TestChatTab({ client }: TabProps) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [session, setSession] = useState(newSession);
  const [selected, setSelected] = useState<number | null>(null);
  const log = useRef<HTMLDivElement>(null);

  useEffect(() => {
    log.current?.scrollTo({ top: log.current.scrollHeight, behavior: "smooth" });
  }, [turns, busy]);

  const send = async (e?: FormEvent) => {
    e?.preventDefault();
    const q = input.trim();
    if (!q || busy) return;
    setInput("");
    setBusy(true);
    const index = turns.length;
    setTurns((t) => [...t, { question: q }]);
    try {
      const answer = await api<Answer>(`/api/admin/clients/${client.client_id}/test-chat`, { body: { session_id: session, message: q } });
      setTurns((t) => t.map((x, i) => (i === index ? { ...x, answer } : x)));
      setSelected(index);
    } catch (err) {
      setTurns((t) => t.map((x, i) => (i === index ? { ...x, failed: (err as Error).message } : x)));
    } finally {
      setBusy(false);
    }
  };

  const current = selected !== null ? turns[selected]?.answer : undefined;

  return (
    <div className="grid split-chat">
      <Card title="Test chat" subtitle="Same pipeline as the widget. Logged as “test” and excluded from statistics." actions={<Button size="sm" onClick={() => { setTurns([]); setSession(newSession()); setSelected(null); }}>New conversation</Button>} bodyless>
        <div className="chat">
          <div className="chat-log" ref={log} aria-live="polite">
            {client.branding.greeting && <div className="bubble bot">{client.branding.greeting}</div>}
            {turns.map((t, i) => (
              <div key={i} style={{ display: "contents" }}>
                <div className="bubble user">{t.question}</div>
                {t.answer && (
                  <div className="bubble bot" style={{ cursor: "pointer", outline: selected === i ? "2px solid var(--accent-soft)" : undefined }} onClick={() => setSelected(i)}>
                    {t.answer.answer}
                    {t.answer.sources.length > 0 && (
                      <div className="meta">
                        {t.answer.sources.map((s) => s.url ? <a key={s.url} href={s.url} target="_blank" rel="noreferrer">{s.title}</a> : <span key={s.title}>📄 {s.title}</span>)}
                      </div>
                    )}
                    <div className="meta">
                      <Badge color={t.answer.answered ? "green" : "amber"}>{t.answer.answered ? "Answered" : "Unanswered"}</Badge>
                      <span>confidence {t.answer.confidence.toFixed(2)}</span>
                      <span>{t.answer.model_name ?? "no model"}{t.answer.used_fallback ? " (fallback)" : ""}</span>
                      <span>{duration(t.answer.response_ms)}</span>
                    </div>
                  </div>
                )}
                {t.failed && <div className="bubble bot" style={{ color: "var(--danger)" }}>{t.failed}</div>}
              </div>
            ))}
            {busy && <div className="bubble bot"><span className="spinner" /></div>}
          </div>
          <form className="chat-input" onSubmit={send}>
            <input className="input" value={input} onChange={(e) => setInput(e.target.value)} placeholder="Ask a question as a visitor would…" aria-label="Question" maxLength={1000} />
            <Button kind="primary" type="submit" loading={busy}>Send</Button>
          </form>
        </div>
      </Card>

      <Card title="Diagnostics" subtitle="Click an answer to inspect it.">
        {!current ? <Empty title="No answer selected" /> : (
          <div className="stack">
            {current.error && <div className="alert error">{current.error}</div>}
            <dl className="kv">
              <dt>Confidence</dt><dd>{current.confidence.toFixed(3)} <span className="muted">(threshold {current.threshold})</span></dd>
              <dt>Model</dt><dd>{current.model_name ?? "—"}{current.used_fallback && <> <Badge color="amber">fallback used</Badge></>}</dd>
              <dt>Response time</dt><dd>{duration(current.response_ms)}</dd>
              <dt>Tokens</dt><dd>{current.input_tokens} in / {current.output_tokens} out · {money(current.cost)}</dd>
              <dt>Language</dt><dd>{current.language}</dd>
            </dl>
            <div>
              <h3 style={{ marginBottom: 8 }}>Retrieved content ({current.chunks.length})</h3>
              <div className="stack" style={{ gap: 8 }}>
                {current.chunks.map((c, i) => (
                  <div className="chunk" key={i}>
                    <div className="row between">
                      <strong className="truncate" style={{ maxWidth: "70%" }}>{c.title || c.source}</strong>
                      <span className="row" style={{ gap: 4 }}>
                        {c.similarity !== null && <Badge>sim {c.similarity.toFixed(2)}</Badge>}
                        {c.keyword && <Badge color="blue">keyword</Badge>}
                      </span>
                    </div>
                    <div className="small muted truncate">{c.source}</div>
                    <div className="small" style={{ marginTop: 4, whiteSpace: "pre-wrap" }}>{c.preview}{c.preview.length >= 300 ? "…" : ""}</div>
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}
      </Card>
    </div>
  );
}
