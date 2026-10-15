import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, Conflict } from "../api";
import { ErrorText, fmtTime, useAction } from "../ui";

export default function ConflictsPage() {
  const conflicts = useQuery({ queryKey: ["conflicts"], queryFn: api.conflicts });
  return (
    <>
      <h2>PRs</h2>
      <p className="muted">Fields changed both here and in Redmine since the last sync. Pick which value wins.</p>
      <ErrorText error={conflicts.error} />
      {conflicts.data?.length === 0 && <p className="muted empty">No open PRs.</p>}
      {conflicts.data?.map((c) => <ConflictCard key={c.id} c={c} />)}
    </>
  );
}

const show = (v: string | null) => (v === null || v === "" ? <span className="muted">(empty)</span> : v);

export function ConflictCard({ c }: { c: Conflict }) {
  const [reason, setReason] = useState("");
  const resolve = useAction(api.resolve);
  return (
    <section className="card conflict">
      <header>
        <Link to={`/items/${c.item_uuid}`} className="title">{c.item_title}</Link>
        {c.redmine_issue_id && <span className="muted"> #{c.redmine_issue_id}</span>}
        <span className="muted small"> · {c.field} · detected {fmtTime(c.detected_at)}</span>
      </header>
      <table className="cmp">
        <tbody>
          <tr><th>Base</th><td>{show(c.base_value)}</td></tr>
          <tr><th>Server (yours)</th><td>{show(c.server_value)}</td></tr>
          <tr><th>Redmine{c.redmine_author ? ` (${c.redmine_author})` : ""}</th><td>{show(c.redmine_value)}</td></tr>
        </tbody>
      </table>
      <div className="add-row">
        <input placeholder="Reason (required to keep yours; posted as a Redmine note)" value={reason}
               onChange={(e) => setReason(e.target.value)} />
        <button disabled={!reason.trim() || resolve.isPending}
                onClick={() => resolve.mutate([c.id, "keep_server", reason])}>Keep server</button>
        <button className="ghost" disabled={resolve.isPending}
                onClick={() => resolve.mutate([c.id, "take_redmine"])}>Take Redmine</button>
      </div>
      <ErrorText error={resolve.error} />
    </section>
  );
}
