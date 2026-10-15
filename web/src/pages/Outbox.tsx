import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, OutboxRow } from "../api";
import { ErrorText, fmtTime, useAction } from "../ui";

type Filter = "all" | OutboxRow["state"];

export default function OutboxPage() {
  const ob = useQuery({ queryKey: ["outbox"], queryFn: api.outbox });
  const push = useAction(api.push);
  const poll = useAction(api.poll);
  const [filter, setFilter] = useState<Filter>("all");
  const rows = (ob.data?.rows ?? []).filter((r) => filter === "all" || r.state === filter);
  const count = (s: OutboxRow["state"]) => ob.data?.rows.filter((r) => r.state === s).length ?? 0;

  return (
    <>
      <h2>Outbox</h2>
      <div className="meta">
        <span className="muted">Last poll {fmtTime(ob.data?.last_poll_at ?? null)} · last push {fmtTime(ob.data?.last_push_at ?? null)}</span>
        {ob.data?.dry_run && <span className="pill warn">dry-run: Push logs payloads, sends nothing</span>}
      </div>
      <div className="add-row">
        <button onClick={() => poll.mutate([])} disabled={poll.isPending}>{poll.isPending ? "Polling…" : "Poll Redmine now"}</button>
        <button onClick={() => push.mutate([])} disabled={push.isPending}>{push.isPending ? "Pushing…" : "Push now"}</button>
      </div>
      <ErrorText error={ob.error ?? push.error ?? poll.error} />
      {push.data && (
        <p className={push.data.errors.length ? "error" : "ok"}>
          {push.data.dry_run ? `Dry-run: ${push.data.payloads.length} payload(s) logged.`
            : `Sent ${push.data.sent}, ${push.data.errors.length} error(s).`}
        </p>
      )}

      <div className="tabs">
        {(["all", "pending", "error", "pushed"] as Filter[]).map((f) => (
          <button key={f} className={f === filter ? "tab active" : "tab"} onClick={() => setFilter(f)}>
            {f}{f !== "all" && <span className="muted small"> {count(f)}</span>}
          </button>
        ))}
      </div>
      {rows.length === 0 && <p className="muted empty">Nothing here.</p>}
      <ul className="list">
        {rows.map((r) => (
          <li key={r.id} className="row column">
            <div className="meta">
              <span className={`pill ${r.state === "error" ? "danger" : r.state === "pushed" ? "okpill" : ""}`}>{r.state}</span>
              <Link to={`/items/${r.item_uuid}`}>{r.item_title}</Link>
              <span className="muted small">queued {fmtTime(r.created_at)}{r.pushed_at && ` · pushed ${fmtTime(r.pushed_at)}`}</span>
            </div>
            {r.error && <p className="error small">{r.error}</p>}
            <details><summary className="small muted">payload</summary><pre>{JSON.stringify(r.payload, null, 2)}</pre></details>
          </li>
        ))}
      </ul>

      {(ob.data?.pending_notes.length || ob.data?.pending_hours.length) ? (
        <section className="card">
          <h3>Waiting notes &amp; hours</h3>
          <ul className="events">
            {ob.data.pending_notes.map((n) => (
              <li key={`n${n.id}`}>Note on <b>{n.item_title}</b>: {n.body.slice(0, 80)}
                {n.error && <p className="error small">{n.error}</p>}</li>
            ))}
            {ob.data.pending_hours.map((h) => (
              <li key={`h${h.id}`}>{h.hours} h on <b>{h.item_title}</b>
                {h.error && <p className="error small">{h.error}</p>}</li>
            ))}
          </ul>
        </section>
      ) : null}
    </>
  );
}
