import { useEffect, useState } from "react";
import { api, type User } from "../api";
import { useAuth } from "../auth";
import { PageHead } from "../components/Layout";
import { Alert, Badge, Button, Card, Field, Loading, Modal } from "../components/ui";
import { relative } from "../format";

interface UsersData {
  users: User[];
  clients: { id: number; client_id: string; name: string }[];
}

export function Users() {
  const { user: me } = useAuth();
  const [data, setData] = useState<UsersData | null>(null);
  const [editing, setEditing] = useState<User | "new" | null>(null);
  const load = () => api<UsersData>("/api/admin/users").then(setData);
  useEffect(() => {
    load();
  }, []);
  if (!data) return <div className="page"><Loading /></div>;
  return (
    <div className="page stack">
      <PageHead title="Users" description="Super admins manage everything. Client admins only see their assigned clients." actions={<Button kind="primary" icon="plus" onClick={() => setEditing("new")}>Add user</Button>} />
      <Card bodyless>
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>Name</th><th>Role</th><th>Clients</th><th>Status</th><th>Last sign-in</th><th /></tr></thead>
            <tbody>
              {data.users.map((u) => (
                <tr key={u.id}>
                  <td><strong>{u.name}</strong>{u.id === me?.id && <span className="muted"> (you)</span>}<div className="small muted">{u.email}</div></td>
                  <td>{u.role === "super_admin" ? <Badge color="teal">Super admin</Badge> : <Badge>Client admin</Badge>}</td>
                  <td className="small">{u.role === "super_admin" ? <span className="muted">All</span> : u.clients.map((c) => c.name).join(", ") || <span className="muted">None</span>}</td>
                  <td>{u.active ? <Badge color="green"><span className="dot" />Active</Badge> : <Badge color="red"><span className="dot" />Disabled</Badge>}</td>
                  <td className="small muted">{relative(u.last_login_at)}</td>
                  <td className="num"><Button size="sm" onClick={() => setEditing(u)}>Edit</Button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
      {editing && <UserForm user={editing === "new" ? null : editing} clients={data.clients} isMe={editing !== "new" && editing.id === me?.id} onClose={() => setEditing(null)} onSaved={(d) => { setData(d); setEditing(null); }} />}
    </div>
  );
}

function UserForm({ user, clients, isMe, onClose, onSaved }: { user: User | null; clients: UsersData["clients"]; isMe: boolean; onClose: () => void; onSaved: (d: UsersData) => void }) {
  const [name, setName] = useState(user?.name ?? "");
  const [email, setEmail] = useState(user?.email ?? "");
  const [role, setRole] = useState<User["role"]>(user?.role ?? "client_admin");
  const [active, setActive] = useState(user?.active ?? true);
  const [password, setPassword] = useState("");
  const [clientIds, setClientIds] = useState<number[]>(user?.clients.map((c) => c.id) ?? []);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const save = async () => {
    setBusy(true);
    setError("");
    try {
      const body = { name, email, role, client_ids: clientIds, ...(user ? { active, password: password || undefined } : { password }) };
      const d = user ? await api<UsersData>(`/api/admin/users/${user.id}`, { method: "PATCH", body }) : await api<UsersData>("/api/admin/users", { body });
      onSaved(d);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title={user ? `Edit ${user.name}` : "Add user"} onClose={onClose} footer={<><Button onClick={onClose}>Cancel</Button><Button kind="primary" loading={busy} onClick={save}>Save</Button></>}>
      <div className="stack">
        {error && <Alert kind="error">{error}</Alert>}
        <Field label="Name" htmlFor="u-name"><input id="u-name" className="input" value={name} onChange={(e) => setName(e.target.value)} /></Field>
        <Field label="Email" htmlFor="u-email"><input id="u-email" className="input" type="email" value={email} onChange={(e) => setEmail(e.target.value)} /></Field>
        <Field label={user ? "New password" : "Password"} htmlFor="u-pass" hint={user ? "Leave blank to keep the current password. Changing it signs the user out." : "At least 10 characters."}>
          <input id="u-pass" className="input" type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} />
        </Field>
        <Field label="Role" htmlFor="u-role">
          <select id="u-role" className="select" value={role} onChange={(e) => setRole(e.target.value as User["role"])} disabled={isMe}>
            <option value="client_admin">Client admin: assigned clients only (documents, branding, test chat, stats)</option>
            <option value="super_admin">Super admin: everything, including AI models and settings</option>
          </select>
        </Field>
        {role === "client_admin" && (
          <div className="field">
            <span className="label">Assigned clients</span>
            {clients.length === 0 && <span className="hint">No clients yet.</span>}
            {clients.map((c) => (
              <label key={c.id} className="check">
                <input type="checkbox" checked={clientIds.includes(c.id)} onChange={(e) => setClientIds(e.target.checked ? [...clientIds, c.id] : clientIds.filter((x) => x !== c.id))} />
                {c.name} <span className="muted mono small">{c.client_id}</span>
              </label>
            ))}
          </div>
        )}
        {user && !isMe && (
          <label className="check"><input type="checkbox" checked={active} onChange={(e) => setActive(e.target.checked)} />Account active</label>
        )}
      </div>
    </Modal>
  );
}
