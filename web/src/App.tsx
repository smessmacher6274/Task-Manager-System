import { useQuery, useQueryClient } from "@tanstack/react-query";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { api } from "./api";
import Login from "./pages/Login";
import ItemsPage from "./pages/Items";
import TriagePage from "./pages/Triage";
import ConflictsPage from "./pages/Conflicts";
import ItemView from "./pages/ItemView";
import OutboxPage from "./pages/Outbox";

export default function App() {
  const qc = useQueryClient();
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  const conflicts = useQuery({ queryKey: ["conflicts"], queryFn: api.conflicts, enabled: me.isSuccess });
  const triage = useQuery({ queryKey: ["triage"], queryFn: api.triageQueue, enabled: me.isSuccess });

  if (me.isLoading) return <div className="center muted">Loading…</div>;
  if (!me.isSuccess) return <Login />;

  const logout = async () => {
    await api.logout();
    qc.clear();
    qc.invalidateQueries({ queryKey: ["me"] });
  };

  return (
    <div className="shell">
      <header className="topbar">
        <span className="brand">Eco</span>
        <nav>
          <NavLink to="/items">Items</NavLink>
          <NavLink to="/triage">Triage{badge(triage.data?.length)}</NavLink>
          <NavLink to="/conflicts">PRs{badge(conflicts.data?.length)}</NavLink>
          <NavLink to="/outbox">Outbox</NavLink>
        </nav>
        <span className="spacer" />
        {me.data.dry_run && <span className="pill warn" title="Outbox pushes are logged, not sent">dry-run</span>}
        <span className="muted">{me.data.name}</span>
        <button className="link" onClick={logout}>Log out</button>
      </header>
      <main>
        <Routes>
          <Route path="/" element={<Navigate to="/items" replace />} />
          <Route path="/items" element={<ItemsPage />} />
          <Route path="/items/:uuid" element={<ItemView />} />
          <Route path="/triage" element={<TriagePage />} />
          <Route path="/conflicts" element={<ConflictsPage />} />
          <Route path="/outbox" element={<OutboxPage />} />
          <Route path="*" element={<Navigate to="/items" replace />} />
        </Routes>
      </main>
    </div>
  );
}

function badge(n?: number) {
  return n ? <span className="badge">{n}</span> : null;
}
