import { BrowserRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { AuthProvider, useAuth } from "./auth";
import { Layout } from "./components/Layout";
import { Loading, ToastProvider } from "./components/ui";
import { ClientDetail } from "./pages/client/ClientDetail";
import { Clients } from "./pages/Clients";
import { Dashboard } from "./pages/Dashboard";
import { Jobs } from "./pages/Jobs";
import { Login } from "./pages/Login";
import { Settings } from "./pages/Settings";
import { Setup } from "./pages/Setup";
import { System } from "./pages/System";
import { Users } from "./pages/Users";

function Routed() {
  const { loading, user, setup_completed, isSuper, mode, onprem_client_id } = useAuth();
  const location = useLocation();
  if (loading) return <div className="center-page"><Loading /></div>;

  // Until setup is finished, everything goes to the wizard.
  if (!setup_completed) {
    return location.pathname === "/setup" ? <Setup /> : <Navigate to="/setup" replace />;
  }
  if (location.pathname === "/setup") return <Navigate to="/" replace />;
  if (!user) return <Login />;

  const superOnly = (el: JSX.Element) => (isSuper ? el : <Navigate to="/" replace />);
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Dashboard />} />
        {/* On-premise installs have exactly one client: skip the list. */}
        <Route path="clients" element={mode === "onprem" && onprem_client_id ? <Navigate to={`/clients/${onprem_client_id}`} replace /> : <Clients />} />
        <Route path="clients/:clientId" element={<ClientDetail />} />
        <Route path="clients/:clientId/:tab" element={<ClientDetail />} />
        <Route path="jobs" element={<Jobs />} />
        <Route path="settings" element={superOnly(<Settings />)} />
        <Route path="users" element={superOnly(<Users />)} />
        <Route path="system" element={superOnly(<System />)} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}

export function App() {
  return (
    <BrowserRouter>
      <ToastProvider>
        <AuthProvider>
          <Routed />
        </AuthProvider>
      </ToastProvider>
    </BrowserRouter>
  );
}
