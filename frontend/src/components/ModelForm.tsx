// Add/edit an AI model, with provider presets, a model list fetched with the API key and a live "Test connection".
import { useEffect, useMemo, useRef, useState } from "react";
import type { AIModel, Provider } from "../api";
import { duration } from "../format";
import { Alert, Button, Field } from "./ui";

export interface ModelValues {
  name: string;
  provider: string;
  base_url: string;
  model_name: string;
  api_key: string;
  temperature: number;
  max_tokens: number;
  timeout_seconds: number;
  cost_input_per_m: string;
  cost_output_per_m: string;
}

export interface TestResult {
  ok: boolean;
  reply?: string;
  error?: string;
  response_ms?: number;
}

export interface DiscoverResult {
  ok: boolean;
  models?: string[];
  error?: string;
}

export function emptyModel(provider?: Provider): ModelValues {
  return {
    name: "",
    provider: provider?.key ?? "openai",
    base_url: provider?.default_base_url ?? "",
    model_name: "",
    api_key: "",
    temperature: 0.2,
    max_tokens: 600,
    timeout_seconds: 60,
    cost_input_per_m: "",
    cost_output_per_m: "",
  };
}

export function modelToValues(m: AIModel): ModelValues {
  return {
    name: m.name,
    provider: m.provider,
    base_url: m.base_url ?? "",
    model_name: m.model_name,
    api_key: "",
    temperature: m.temperature,
    max_tokens: m.max_tokens,
    timeout_seconds: m.timeout_seconds,
    cost_input_per_m: m.cost_input_per_m?.toString() ?? "",
    cost_output_per_m: m.cost_output_per_m?.toString() ?? "",
  };
}

export function payload(v: ModelValues) {
  return {
    ...v,
    cost_input_per_m: v.cost_input_per_m === "" ? null : Number(v.cost_input_per_m),
    cost_output_per_m: v.cost_output_per_m === "" ? null : Number(v.cost_output_per_m),
  };
}

// Model lists already fetched in this browser tab, so reopening the form is instant.
const listCache = new Map<string, string[]>();

function isLoopback(url: string): boolean {
  try {
    const host = new URL(url).hostname;
    return host === "localhost" || host.startsWith("127.") || host === "[::1]";
  } catch {
    return false;
  }
}

export function ModelForm({
  values, onChange, providers, mode, editing, hasStoredKey, onTest, testResult, testing, onDiscover,
}: {
  values: ModelValues;
  onChange: (v: ModelValues) => void;
  providers: Provider[];
  mode: string;
  editing?: boolean;
  hasStoredKey?: boolean;
  onTest: () => void;
  testResult: TestResult | null;
  testing: boolean;
  onDiscover: (refresh: boolean) => Promise<DiscoverResult>;
}) {
  const provider = useMemo(() => providers.find((p) => p.key === values.provider), [providers, values.provider]);
  const [advanced, setAdvanced] = useState(false);
  const [found, setFound] = useState<string[] | null>(null);
  const [manual, setManual] = useState(false);
  const [fetching, setFetching] = useState(false);
  const [fetchError, setFetchError] = useState("");
  const set = <K extends keyof ModelValues>(key: K, value: ModelValues[K]) => onChange({ ...values, [key]: value });

  const chooseProvider = (key: string) => {
    const p = providers.find((x) => x.key === key);
    const oldDefault = provider?.default_base_url ?? "";
    // Replace the URL only if the user hadn't customised it.
    const base_url = !values.base_url || values.base_url === oldDefault ? p?.default_base_url ?? "" : values.base_url;
    onChange({ ...values, provider: key, base_url, name: values.name || p?.label.split(" (")[0] || "" });
    setFound(null);
    setFetchError("");
  };

  // Latest props for the debounced auto-fetch, which must not restart on every render.
  const latest = useRef({ values, onChange, onDiscover, provider });
  latest.current = { values, onChange, onDiscover, provider };
  const requestNo = useRef(0);
  const pasted = useRef(false); // a pasted key is complete: fetch at once instead of waiting for typing to stop

  const fetchModels = async (refresh = false) => {
    const no = ++requestNo.current;
    const v = latest.current.values;
    const cacheKey = `${v.provider}\n${v.base_url}\n${v.api_key}`;
    const cached = refresh ? undefined : listCache.get(cacheKey);
    setFetching(!cached);
    setFetchError("");
    try {
      const r: DiscoverResult = cached ? { ok: true, models: cached } : await latest.current.onDiscover(refresh);
      if (r.ok && r.models?.length) listCache.set(cacheKey, r.models);
      if (no !== requestNo.current) return; // a newer key/URL was entered meanwhile
      const { values, onChange, provider } = latest.current;
      if (!r.ok) {
        setFetchError(r.error ?? "Could not fetch the model list.");
      } else if (!r.models?.length) {
        setFetchError("The provider returned no models for this key.");
      } else {
        setFound(r.models);
        setManual(false);
        if (!r.models.includes(values.model_name)) {
          const preferred = provider && r.models.includes(provider.example_model) ? provider.example_model : r.models[0];
          onChange({ ...values, model_name: preferred });
        }
      }
    } catch (e) {
      if (no === requestNo.current) setFetchError((e as Error).message);
    } finally {
      if (no === requestNo.current) setFetching(false);
    }
  };

  // Load the model list automatically once a key (or, for keyless local servers, a URL) is entered.
  const canFetch = provider?.needs_api_key ? values.api_key.trim().length >= 8 || !!hasStoredKey : !!values.base_url.trim();
  useEffect(() => {
    if (!canFetch) {
      requestNo.current++;
      setFetching(false);
      setFound(null);
      return;
    }
    const timer = setTimeout(fetchModels, pasted.current ? 0 : 300);
    pasted.current = false;
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [values.provider, values.base_url, values.api_key, canFetch]);
  const showList = found !== null && !manual;

  return (
    <div className="stack">
      <div className="form-grid">
        <Field label="Provider" htmlFor="m-provider">
          <select id="m-provider" className="select" value={values.provider} onChange={(e) => chooseProvider(e.target.value)}>
            <optgroup label="Cloud">
              {providers.filter((p) => p.cloud).map((p) => <option key={p.key} value={p.key}>{p.label}</option>)}
            </optgroup>
            <optgroup label="Local / self-hosted">
              {providers.filter((p) => !p.cloud).map((p) => <option key={p.key} value={p.key}>{p.label}</option>)}
            </optgroup>
          </select>
        </Field>
        <Field label="Display name" htmlFor="m-name" hint="How this model appears in lists.">
          <input id="m-name" className="input" value={values.name} onChange={(e) => set("name", e.target.value)} placeholder="e.g. Qwen on Bionic" />
        </Field>
        <Field label="Base URL" htmlFor="m-url" full hint={provider?.openai_compatible ? "OpenAI-compatible endpoint, usually ending in /v1." : "Pre-filled for this provider; change only if you use a proxy."}>
          <input id="m-url" className="input" value={values.base_url} onChange={(e) => set("base_url", e.target.value)} placeholder={provider?.default_base_url || "http://host.docker.internal:8000/v1"} />
        </Field>
        <Field
          label="Model name"
          htmlFor="m-model"
          hint={showList
            ? <>{found!.length} models available with this key. <button type="button" className="link" onClick={() => setManual(true)}>Type a name instead</button></>
            : fetching ? "Loading the models this key can use…"
            : provider?.needs_api_key && !canFetch ? "Enter the API key to choose from the provider's models."
            : <>Exactly as the provider names it, e.g. {provider?.example_model ?? "model-name"}.</>}
        >
          <div className="row" style={{ flexWrap: "nowrap" }}>
            {showList ? (
              <select id="m-model" className="select" value={values.model_name} onChange={(e) => set("model_name", e.target.value)}>
                {found!.map((m) => <option key={m} value={m}>{m}</option>)}
              </select>
            ) : (
              <input id="m-model" className="input" value={values.model_name} onChange={(e) => set("model_name", e.target.value)} placeholder={provider?.example_model} />
            )}
            <Button icon="refresh" onClick={() => fetchModels(true)} loading={fetching} disabled={!canFetch} aria-label="Refresh model list">Refresh</Button>
          </div>
        </Field>
        <Field
          label={`API key${provider?.needs_api_key ? "" : " (optional)"}`}
          htmlFor="m-key"
          hint={editing && hasStoredKey ? "Leave blank to keep the saved key. Stored encrypted." : "Stored encrypted; never shown again."}
        >
          <input id="m-key" className="input" type="password" autoComplete="new-password" value={values.api_key} onPaste={() => { pasted.current = true; }} onChange={(e) => set("api_key", e.target.value)} placeholder={editing && hasStoredKey ? "••••••••" : ""} />
        </Field>
      </div>

      {fetchError && <Alert kind="error">{fetchError}</Alert>}
      {mode === "onprem" && provider?.cloud && (
        <Alert kind="warning">This is a cloud provider. Visitor questions and your website/document content will be sent outside this server.</Alert>
      )}
      {isLoopback(values.base_url) && (
        <Alert kind="warning">
          <strong>localhost</strong> inside Docker means the container itself. To reach a model running on the host machine, use <code>host.docker.internal</code> instead.
        </Alert>
      )}

      <button type="button" className="btn ghost sm" style={{ alignSelf: "flex-start", paddingLeft: 0 }} onClick={() => setAdvanced(!advanced)}>
        {advanced ? "Hide" : "Show"} advanced options
      </button>
      {advanced && (
        <div className="form-grid">
          <Field label="Temperature" htmlFor="m-temp" hint="0 = precise, 1 = creative. 0.1–0.3 suits factual answers.">
            <input id="m-temp" className="input" type="number" min={0} max={2} step={0.1} value={values.temperature} onChange={(e) => set("temperature", Number(e.target.value))} />
          </Field>
          <Field label="Max answer tokens" htmlFor="m-max">
            <input id="m-max" className="input" type="number" min={16} max={32000} value={values.max_tokens} onChange={(e) => set("max_tokens", Number(e.target.value))} />
          </Field>
          <Field label="Timeout (seconds)" htmlFor="m-timeout" hint="After this, the fallback model is used (if set).">
            <input id="m-timeout" className="input" type="number" min={5} max={600} value={values.timeout_seconds} onChange={(e) => set("timeout_seconds", Number(e.target.value))} />
          </Field>
          <div />
          <Field label="Cost per 1M input tokens (USD)" htmlFor="m-cin" hint="Optional. Overrides built-in prices; 0 for local models.">
            <input id="m-cin" className="input" type="number" min={0} step="0.01" value={values.cost_input_per_m} onChange={(e) => set("cost_input_per_m", e.target.value)} />
          </Field>
          <Field label="Cost per 1M output tokens (USD)" htmlFor="m-cout">
            <input id="m-cout" className="input" type="number" min={0} step="0.01" value={values.cost_output_per_m} onChange={(e) => set("cost_output_per_m", e.target.value)} />
          </Field>
        </div>
      )}

      <div className="row">
        <Button icon="play" onClick={onTest} loading={testing} disabled={!values.model_name}>Test connection</Button>
        {testResult && (testResult.ok ? <span className="badge green"><span className="dot" />Connected · {duration(testResult.response_ms ?? 0)}</span> : <span className="badge red"><span className="dot" />Failed</span>)}
      </div>
      {testResult && (testResult.ok ? (
        <Alert kind="success"><strong>Reply:</strong> {testResult.reply}</Alert>
      ) : (
        <Alert kind="error">{testResult.error}</Alert>
      ))}
    </div>
  );
}
