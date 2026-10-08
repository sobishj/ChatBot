// App shell: sidebar navigation (role-aware) + content outlet.
import { useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { useAuth } from "../auth";
import { Icon } from "./icons";

export function Layout() {
  const { user, isSuper, mode, onprem_client_id, logout } = useAuth();
  const [open, setOpen] = useState(false);
  const onprem = mode === "onprem";
  const singleClient = onprem && onprem_client_id;

  const links: { to: string; label: string; icon: string; show: boolean }[] = [
    { to: "/", label: "Dashboard", icon: "dashboard", show: true },
    { to: singleClient ? `/clients/${onprem_client_id}` : "/clients", label: singleClient ? "Assistant" : "Clients", icon: "clients", show: true },
    { to: "/jobs", label: "Jobs", icon: "jobs", show: true },
  ];
  const admin = [
    { to: "/settings", label: "Settings", icon: "settings" },
    { to: "/users", label: "Users", icon: "users" },
    { to: "/system", label: "System health", icon: "health" },
  ];

  return (
    <div className="shell">
      <aside className={`sidebar ${open ? "open" : ""}`} onClick={() => setOpen(false)}>
        <div className="brand">
          <span className="brand-mark"><Icon name="chat" size={17} /></span>
          Website Assistant
        </div>
        {links.filter((l) => l.show).map((l) => (
          <NavLink key={l.to} to={l.to} end={l.to === "/"} className={({ isActive }) => `nav-link ${isActive ? "active" : ""}`}>
            <Icon name={l.icon} />{l.label}
          </NavLink>
        ))}
        {isSuper && (
          <>
            <div className="nav-section">Administration</div>
            {admin.map((l) => (
              <NavLink key={l.to} to={l.to} className={({ isActive }) => `nav-link ${isActive ? "active" : ""}`}>
                <Icon name={l.icon} />{l.label}
              </NavLink>
            ))}
          </>
        )}
        <div className="sidebar-foot">
          <div className="who">{user?.name}</div>
          <div className="role">{user?.role === "super_admin" ? "Super admin" : "Client admin"} · {onprem ? "On-premise" : "Cloud"}</div>
          <button className="btn ghost sm" style={{ marginTop: 8, paddingLeft: 0 }} onClick={logout}>
            <Icon name="logout" />Sign out
          </button>
        </div>
      </aside>
      <div className="main">
        <div className="topbar">
          <button className="btn ghost icon-btn" aria-label="Open menu" onClick={() => setOpen(true)}><Icon name="menu" /></button>
          <strong>Website Assistant</strong>
        </div>
        <Outlet />
      </div>
    </div>
  );
}

export function PageHead({ title, description, actions, crumbs }: { title: string; description?: string; actions?: React.ReactNode; crumbs?: React.ReactNode }) {
  return (
    <div className="page-head">
      <div>
        {crumbs && <div className="crumbs">{crumbs}</div>}
        <h1>{title}</h1>
        {description && <p>{description}</p>}
      </div>
      {actions && <div className="row">{actions}</div>}
    </div>
  );
}
