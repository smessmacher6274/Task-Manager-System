import { FormEvent, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api, Item } from "../api";
import { DueLabel, ErrorText, fmtDate, fmtTime, today, useAction } from "../ui";

export default function ItemView() {
  const { uuid = "" } = useParams();
  const item = useQuery({ queryKey: ["item", uuid], queryFn: () => api.item(uuid) });
  if (item.error) return <ErrorText error={item.error} />;
  if (!item.data) return <p className="muted">Loading…</p>;
  const it = item.data;
  return (
    <>
      <p><Link to={`/items?tab=${encodeURIComponent(it.category_key)}`}>← {it.category}</Link></p>
      <h2>{it.title}</h2>
      <div className="meta">
        <DueLabel item={it} />
        <span className="pill">{it.status.replace("_", " ")}</span>
        {it.redmine_issue_id && <span className="muted">Redmine #{it.redmine_issue_id}</span>}
        {it.needs_triage && <Link to="/triage" className="pill warn">needs triage</Link>}
        {it.deleted_at && <span className="pill danger">deleted</span>}
      </div>
      {it.description && <p className="desc">{it.description}</p>}
      <div className="grid2">
        <Thread uuid={uuid} />
        <div>
          <Deadlines item={it} />
          {it.kind === "work" && <Hours uuid={uuid} />}
        </div>
      </div>
    </>
  );
}

function Thread({ uuid }: { uuid: string }) {
  const thread = useQuery({ queryKey: ["thread", uuid], queryFn: () => api.thread(uuid) });
  const [body, setBody] = useState("");
  const add = useAction(api.addNote);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    add.mutate([uuid, body], { onSuccess: () => setBody("") });
  };
  return (
    <section className="card">
      <h3>Notes</h3>
      <ErrorText error={thread.error} />
      {thread.data?.length === 0 && <p className="muted">No notes yet.</p>}
      <ol className="thread">
        {thread.data?.map((n) => (
          <li key={`${n.kind}-${n.id}`} className={n.kind}>
            <div className="small muted">
              {n.author} · {fmtTime(n.at)}
              {n.kind === "note" && (n.pushed ? " · in Redmine (locked)" : n.error ? "" : " · not pushed yet")}
              {n.private && " · private"}
            </div>
            <div className="body">{n.body}</div>
            {n.error && <p className="error small">{n.error}</p>}
          </li>
        ))}
      </ol>
      <form onSubmit={submit} className="stack">
        <textarea rows={3} placeholder="Add a note (becomes a Redmine journal entry at the next push)"
                  value={body} onChange={(e) => setBody(e.target.value)} required />
        <button type="submit" disabled={add.isPending}>Add note</button>
        <ErrorText error={add.error} />
      </form>
    </section>
  );
}

function Deadlines({ item }: { item: Item }) {
  const events = useQuery({ queryKey: ["deadlines", item.uuid], queryFn: () => api.deadlineEvents(item.uuid) });
  const [due, setDue] = useState("");
  const [reason, setReason] = useState("");
  const patch = useAction(api.patchItem);
  const extending = !!due && !!item.due_date && due > item.due_date;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    patch.mutate([item.uuid, { due_date: due, ...(reason ? { reason } : {}) }],
                 { onSuccess: () => { setDue(""); setReason(""); } });
  };
  return (
    <section className="card">
      <h3>Deadline</h3>
      <ul className="events">
        {events.data?.map((e) => (
          <li key={e.id}>
            <span className={`pill ev-${e.event}`}>{e.event}</span>{" "}
            {e.event === "extended" ? <>{fmtDate(e.old_due)} → {fmtDate(e.new_due)}</> : fmtDate(e.new_due)}
            <span className="muted small"> · {e.origin} · {fmtTime(e.ts)}</span>
            {e.reason && <div className="small">“{e.reason}”</div>}
          </li>
        ))}
      </ul>
      {!item.deleted_at && (
        <form onSubmit={submit} className="stack">
          <input type="date" value={due} onChange={(e) => setDue(e.target.value)} required aria-label="New deadline" />
          {extending && <input placeholder="Reason for extension (required)" value={reason}
                               onChange={(e) => setReason(e.target.value)} required />}
          <button type="submit" disabled={patch.isPending}>{extending ? "Extend" : "Set deadline"}</button>
          <ErrorText error={patch.error} />
        </form>
      )}
    </section>
  );
}

function Hours({ uuid }: { uuid: string }) {
  const entries = useQuery({ queryKey: ["hours", uuid], queryFn: () => api.timeEntries(uuid) });
  const lookups = useQuery({ queryKey: ["lookups"], queryFn: api.lookups, staleTime: 5 * 60_000 });
  const [hours, setHours] = useState("");
  const [day, setDay] = useState(today());
  const [activity, setActivity] = useState("");
  const [comment, setComment] = useState("");
  const add = useAction(api.addTime);
  const total = entries.data?.reduce((s, e) => s + e.hours, 0) ?? 0;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    add.mutate([uuid, { hours: Number(hours), spent_on: day, activity_id: Number(activity), comment }],
               { onSuccess: () => { setHours(""); setComment(""); } });
  };
  return (
    <section className="card">
      <h3>Hours <span className="muted small">{total.toFixed(2)} h total</span></h3>
      <ul className="events">
        {entries.data?.map((e) => (
          <li key={e.id}>
            {e.hours} h on {fmtDate(e.spent_on)}
            <span className="muted small"> · {lookups.data?.activities.find((a) => a.id === e.activity_id)?.name ?? e.activity_id}
              {e.pushed ? " · in Redmine" : " · not pushed yet"}</span>
            {e.comment && <div className="small">{e.comment}</div>}
            {e.error && <p className="error small">{e.error}</p>}
          </li>
        ))}
      </ul>
      <form onSubmit={submit} className="stack">
        <div className="add-row">
          <input type="number" step="0.25" min="0.25" placeholder="Hours" value={hours}
                 onChange={(e) => setHours(e.target.value)} required aria-label="Hours" />
          <input type="date" value={day} onChange={(e) => setDay(e.target.value)} required aria-label="Spent on" />
          <select value={activity} onChange={(e) => setActivity(e.target.value)} required aria-label="Activity">
            <option value="">Activity…</option>
            {lookups.data?.activities.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
          </select>
        </div>
        <input placeholder="Comment" value={comment} onChange={(e) => setComment(e.target.value)} />
        <button type="submit" disabled={add.isPending}>Log hours</button>
        <ErrorText error={add.error} />
      </form>
    </section>
  );
}
