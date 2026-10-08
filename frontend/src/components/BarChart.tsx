// Stacked daily bar chart (answered + unanswered) with hover tooltip, legend and a table view.
import { useMemo, useState } from "react";
import { num } from "../format";
import { Segmented } from "./ui";

type Day = { date: string; total: number; unanswered: number };

const HEIGHT = 200;
const PAD = { top: 10, right: 8, bottom: 24, left: 34 };

function niceMax(v: number): number {
  if (v <= 4) return 4;
  const pow = 10 ** Math.floor(Math.log10(v));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * pow).find((s) => v <= s * 4) ?? pow * 10;
  return step * 4;
}

export function DailyChart({ data, title = "Questions per day" }: { data: Day[]; title?: string }) {
  const [hover, setHover] = useState<number | null>(null);
  const [view, setView] = useState<"chart" | "table">("chart");
  const max = useMemo(() => niceMax(Math.max(0, ...data.map((d) => d.total))), [data]);
  const width = 760;
  const innerW = width - PAD.left - PAD.right;
  const innerH = HEIGHT - PAD.top - PAD.bottom;
  const slot = innerW / Math.max(1, data.length);
  const barW = Math.max(2, Math.min(28, slot - 2)); // 2px surface gap between bars
  const y = (v: number) => PAD.top + innerH - (v / max) * innerH;
  const ticks = [0, max / 4, max / 2, (3 * max) / 4, max];
  const labelEvery = Math.ceil(data.length / 8);

  return (
    <div className="stack" style={{ gap: 10 }}>
      <div className="row between">
        <h3>{title}</h3>
        <div className="row">
          <div className="legend" aria-hidden={view === "table"}>
            <span><i style={{ background: "var(--series-1)" }} />Answered</span>
            <span><i style={{ background: "var(--series-2)" }} />Unanswered</span>
          </div>
          <Segmented options={[{ value: "chart", label: "Chart" }, { value: "table", label: "Table" }]} value={view} onChange={setView} />
        </div>
      </div>
      {view === "table" ? (
        <div className="table-wrap" style={{ maxHeight: 260, overflow: "auto" }}>
          <table className="table">
            <thead><tr><th>Date</th><th className="num">Answered</th><th className="num">Unanswered</th><th className="num">Total</th></tr></thead>
            <tbody>
              {[...data].reverse().map((d) => (
                <tr key={d.date}><td>{d.date}</td><td className="num">{num(d.total - d.unanswered)}</td><td className="num">{num(d.unanswered)}</td><td className="num">{num(d.total)}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="chart" onMouseLeave={() => setHover(null)}>
          <svg viewBox={`0 0 ${width} ${HEIGHT}`} role="img" aria-label={`${title}: bar chart, ${data.length} days`}>
            {ticks.map((t) => (
              <g key={t}>
                <line className="gridline" x1={PAD.left} x2={width - PAD.right} y1={y(t)} y2={y(t)} />
                <text className="axis-label" x={PAD.left - 6} y={y(t) + 4} textAnchor="end">{Math.round(t)}</text>
              </g>
            ))}
            {data.map((d, i) => {
              const x = PAD.left + i * slot + (slot - barW) / 2;
              const answered = d.total - d.unanswered;
              const hA = (answered / max) * innerH;
              const hU = (d.unanswered / max) * innerH;
              const base = PAD.top + innerH;
              const r = Math.min(4, barW / 2);
              return (
                <g key={d.date} opacity={hover === null || hover === i ? 1 : 0.55}>
                  {/* Hit target: the whole column, larger than the mark. */}
                  <rect x={PAD.left + i * slot} y={PAD.top} width={slot} height={innerH} fill="transparent" onMouseEnter={() => setHover(i)} />
                  {answered > 0 && (
                    <path
                      d={roundedTop(x, base - hA, barW, hA, d.unanswered > 0 ? 0 : r)}
                      fill="var(--series-1)"
                      pointerEvents="none"
                    />
                  )}
                  {d.unanswered > 0 && (
                    <path d={roundedTop(x, base - hA - hU - (answered > 0 ? 2 : 0), barW, hU, r)} fill="var(--series-2)" pointerEvents="none" />
                  )}
                  {i % labelEvery === 0 && (
                    <text className="axis-label" x={x + barW / 2} y={HEIGHT - 6} textAnchor="middle">{d.date.slice(5)}</text>
                  )}
                </g>
              );
            })}
          </svg>
          {hover !== null && data[hover] && (
            <div className="chart-tip" style={{ left: `${((PAD.left + hover * slot + slot / 2) / width) * 100}%`, top: `${(y(data[hover].total) / HEIGHT) * 100}%` }}>
              <strong>{data[hover].date}</strong><br />
              Answered: {num(data[hover].total - data[hover].unanswered)}<br />
              Unanswered: {num(data[hover].unanswered)}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** Bar path with rounded top corners only (data end), flat at the baseline. */
function roundedTop(x: number, y: number, w: number, h: number, r: number): string {
  if (h <= 0) return "";
  const rr = Math.min(r, h, w / 2);
  return `M${x},${y + h} L${x},${y + rr} Q${x},${y} ${x + rr},${y} L${x + w - rr},${y} Q${x + w},${y} ${x + w},${y + rr} L${x + w},${y + h} Z`;
}

/** Horizontal share bars (languages, models). Single series: no legend needed. */
export function ShareBars({ items }: { items: { label: string; value: number }[] }) {
  const total = items.reduce((s, i) => s + i.value, 0) || 1;
  return (
    <div className="bar-list">
      {items.map((i) => (
        <div className="item" key={i.label}>
          <span className="truncate" title={i.label}>{i.label}</span>
          <div className="track"><div className="fill" style={{ width: `${(i.value / total) * 100}%` }} /></div>
          <span className="num" style={{ textAlign: "right" }}>{num(i.value)}</span>
        </div>
      ))}
    </div>
  );
}
