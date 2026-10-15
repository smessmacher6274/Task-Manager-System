import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, Item, Lookups } from "../api";
import { DueLabel, ErrorText, fmtTime, useAction } from "../ui";

export default function TriagePage() {
  const queue = useQuery({ queryKey: ["triage"], queryFn: api.triageQueue });
  const lookups = useQuery({ queryKey: ["lookups"], queryFn: api.lookups, staleTime: 5 * 60_000 });

  return (
    <>
      <h2>Triage</h2>
      <p className="muted">Work items from the tablet need a Redmine project and tracker before they can be pushed.</p>
      <ErrorText error={queue.error ?? lookups.error} />
      {queue.data?.length === 0 && <p className="muted empty">Triage queue is empty.</p>}
      <ul className="list">
        {queue.data?.map((it) => <TriageRow key={it.uuid} item={it} lookups={lookups.data} />)}
      </ul>
    </>
  );
}

export function TriageRow({ item, lookups }: { item: Item; lookups?: Lookups }) {
  const [project, setProject] = useState("");
  const [tracker, setTracker] = useState("");
  const save = useAction(api.triage);
  return (
    <li className="row">
      <div className="row-main">
        <span className="title">{item.title}</span>
        <div className="meta">
          <DueLabel item={item} />
          <span className="muted">captured {fmtTime(item.captured_at)} via {item.source}</span>
        </div>
      </div>
      <div className="row-actions">
        <select aria-label="Project" value={project} onChange={(e) => setProject(e.target.value)}>
          <option value="">Project…</option>
          {lookups?.projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
        <select aria-label="Tracker" value={tracker} onChange={(e) => setTracker(e.target.value)}>
          <option value="">Tracker…</option>
          {lookups?.trackers.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
        </select>
        <button disabled={!project || !tracker || save.isPending}
                onClick={() => save.mutate([item.uuid, Number(project), Number(tracker)])}>
          Save
        </button>
      </div>
      <ErrorText error={save.error} />
    </li>
  );
}
