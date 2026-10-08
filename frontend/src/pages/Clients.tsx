import { useEffect, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api, type Client } from "../api";
import { useAuth } from "../auth";
import { PageHead } from "../components/Layout";
import { Alert, Badge, Button, Card, Empty, Field, Loading, Modal } from "../components/ui";
import { num, relative } from "../format";

export function Clients() {
  const { isSuper, mode } = useAuth();
  const navigate = useNavigate();
  const [clients, setClients] = useState<Client[] | null>(null);
  const [adding, setAdding] = useState(false);
  const [filter, setFilter] = useState("");

  const load = () => api<{ clients: Client[] }>("/api/admin/clients").then((d) => setClients(d.clients));
  useEffect(() => {
    load();
  }, []);

  if (!clients) return <div className="page"><Loading /></div>;
  const shown = clients.filter((c) => `${c.name} ${c.client_id} ${c.website_url ?? ""}`.toLowerCase().includes(filter.toLowerCase()));
  const canAdd = isSuper && !(mode === "onprem" && clients.length > 0);

  return (
    <div className="page stack">
      <PageHead
        title="Clients"
        description="Each client is one website with its own content, branding and statistics."
        actions={canAdd && <Button kind="primary" icon="plus" onClick={() => setAdding(true)}>Add client</Button>}
      />
      <Card bodyless>
        {clients.length > 5 && (
          <div className="card-head"><input className="input" style={{ maxWidth: 320 }} placeholder="Search clients…" value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="Search clients" /></div>
        )}
        {clients.length === 0 ? (
          <Empty title="No clients yet">{canAdd ? "Add your first client to start crawling its website." : "Ask a super admin to assign you a client."}</Empty>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr><th>Name</th><th>Status</th><th>Website</th><th>AI model</th><th>Last crawl</th><th className="num">Pages</th><th className="num">Documents</th><th className="num">Questions (30 d)</th></tr>
              </thead>
              <tbody>
                {shown.map((c) => (
                  <tr key={c.id} className="clickable" onClick={() => navigate(`/clients/${c.client_id}`)}>
                    <td><strong>{c.name}</strong><div className="small muted mono">{c.client_id}</div></td>
                    <td>{c.active ? <Badge color="green"><span className="dot" />Active</Badge> : <Badge><span className="dot" />Inactive</Badge>}</td>
                    <td className="small"><span className="truncate" style={{ maxWidth: 220 }}>{c.website_url ?? "—"}</span></td>
                    <td className="small">{c.effective_model_name ?? "—"}</td>
                    <td className="small muted nowrap">{relative(c.last_crawl_at)}</td>
                    <td className="num">{num(c.pages)}</td>
                    <td className="num">{num(c.documents)}</td>
                    <td className="num">{num(c.questions_30d)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      {adding && <AddClient onClose={() => setAdding(false)} onCreated={(c) => navigate(`/clients/${c.client_id}`)} />}
    </div>
  );
}

function AddClient({ onClose, onCreated }: { onClose: () => void; onCreated: (c: Client) => void }) {
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [website, setWebsite] = useState("");
  const [domains, setDomains] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const autoSlug = (n: string) => n.toLowerCase().normalize("NFKD").replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 64);

  const submit = async (e?: FormEvent) => {
    e?.preventDefault();
    setBusy(true);
    setError("");
    try {
      const { client } = await api<{ client: Client }>("/api/admin/clients", {
        body: { client_id: slug, name, website_url: website, allowed_domains: domains.split(/[\s,]+/).filter(Boolean) },
      });
      onCreated(client);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title="Add client" onClose={onClose} footer={<><Button onClick={onClose}>Cancel</Button><Button kind="primary" loading={busy} onClick={() => submit()}>Create client</Button></>}>
      <form className="stack" onSubmit={submit}>
        {error && <Alert kind="error">{error}</Alert>}
        <Field label="Name" htmlFor="n-name"><input id="n-name" className="input" autoFocus required value={name} onChange={(e) => { setName(e.target.value); if (!slug || slug === autoSlug(name)) setSlug(autoSlug(e.target.value)); }} placeholder="Lulu Mall Kochi" /></Field>
        <Field label="Client ID" htmlFor="n-slug" hint="Lowercase letters, digits and hyphens. Appears in the embed code and can't be changed."><input id="n-slug" className="input mono" required value={slug} onChange={(e) => setSlug(e.target.value)} placeholder="lulu-kochi" /></Field>
        <Field label="Website URL" htmlFor="n-web"><input id="n-web" className="input" value={website} onChange={(e) => setWebsite(e.target.value)} placeholder="https://www.example.com" /></Field>
        <Field label="Allowed domains" htmlFor="n-dom" hint="Sites allowed to show the widget, comma separated. Defaults to the website's domain (www and non-www both work).">
          <input id="n-dom" className="input" value={domains} onChange={(e) => setDomains(e.target.value)} placeholder="example.com" />
        </Field>
        <button type="submit" hidden />
      </form>
    </Modal>
  );
}
