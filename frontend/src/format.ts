// Formatting helpers (dates, numbers, sizes, money).

export function dateTime(value: string | null | undefined): string {
  if (!value) return "—";
  const d = new Date(value);
  return d.toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

export function relative(value: string | null | undefined): string {
  if (!value) return "never";
  const diff = (Date.now() - new Date(value).getTime()) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} h ago`;
  if (diff < 86400 * 30) return `${Math.floor(diff / 86400)} d ago`;
  return dateTime(value);
}

export function num(value: number | null | undefined): string {
  return (value ?? 0).toLocaleString();
}

export function pct(value: number): string {
  return `${(value * 100).toFixed(value > 0 && value < 0.1 ? 1 : 0)}%`;
}

export function money(value: number): string {
  if (!value) return "$0.00";
  return value < 0.01 ? `$${value.toFixed(4)}` : `$${value.toFixed(2)}`;
}

export function bytes(value: number): string {
  if (value < 1024) return `${value} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = value / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v < 10 ? 1 : 0)} ${units[i]}`;
}

export function duration(ms: number): string {
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
}
