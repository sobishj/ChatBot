// Session state: who is signed in, whether setup is done, and the install mode.
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api, onApiError, primeCsrf, type User } from "./api";

interface Session {
  user: User | null;
  setup_completed: boolean;
  mode: "cloud" | "onprem";
  onprem_client_id: string | null;
}

interface AuthValue extends Session {
  loading: boolean;
  refresh: () => Promise<void>;
  logout: () => Promise<void>;
  isSuper: boolean;
}

const AuthCtx = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session>({ user: null, setup_completed: true, mode: "cloud", onprem_client_id: null });
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    await primeCsrf();
    try {
      setSession(await api<Session>("/api/admin/auth/me"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    // Session expired or setup not finished: reload session state so routing reacts.
    return onApiError((status, detail) => {
      if (status === 401) setSession((s) => ({ ...s, user: null }));
      if (status === 409 && detail === "setup_required") setSession((s) => ({ ...s, setup_completed: false }));
    });
  }, [refresh]);

  const logout = useCallback(async () => {
    await api("/api/admin/auth/logout", { method: "POST" });
    setSession((s) => ({ ...s, user: null }));
  }, []);

  return (
    <AuthCtx.Provider value={{ ...session, loading, refresh, logout, isSuper: session.user?.role === "super_admin" }}>
      {children}
    </AuthCtx.Provider>
  );
}

export function useAuth(): AuthValue {
  const value = useContext(AuthCtx);
  if (!value) throw new Error("useAuth outside AuthProvider");
  return value;
}
