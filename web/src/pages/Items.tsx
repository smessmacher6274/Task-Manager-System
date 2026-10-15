import { FormEvent, useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { api, Item } from "../api";
import { DueLabel, ErrorText, useAction } from "../ui";

const KIND_ORDER = { work: 0, personal: 1, custom: 2 } as const;

export default function ItemsPage() {
  const cats = useQuery({ queryKey: ["categories"], queryFn: api.categories });
  const [params, setParams] = useSearchParams();
  const sorted = [...(cats.data ?? [])].sort((a, b) => KIND_ORDER[a.kind] - KIND_ORDER[b.kind] || a.name.localeCompare(b.name));
  // Category ids repeat across the work and personal DBs, so tabs use the "<db>:<id>" key.
  const activeKey = params.get("tab") ?? sorted[0]?.key;
  const active = sorted.find((c) => c.key === activeKey);
  const items = useQuery({ queryKey: ["items", activeKey], queryFn: () => api.items(activeKey), enabled: !!active });

  return (
    <>
      <div className="tabs" role="tablist">
        {sorted.map((c) => (
          <button key={c.key} role="tab" aria-selected={c.key === activeKey}
                  className={c.key === activeKey ? "tab active" : "tab"}
                  onClick={() => setParams({ tab: c.key })}>
            {c.name}<span className="muted small"> {c.kind}</span>
          </button>
        ))}
      </div>
      {active && <AddItem categoryKey={active.key} synced={active.syncs_redmine} />}
      <ErrorText error={items.error} />
      {items.data && items.data.length === 0 && <p className="muted empty">Nothing open here.</p>}
      <ul className="list">
        {items.data?.map((it) => <ItemRow key={it.uuid} item={it} />)}
      </ul>
    </>
  );
}

function AddItem({ categoryKey, synced }: { categoryKey: string; synced: boolean }) {
  const [title, setTitle] = useState("");
  const [due, setDue] = useState("");
  const create = useAction(api.createItem);
  useEffect(() => create.reset(), [categoryKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const submit = (e: FormEvent) => {
    e.preventDefault();
    create.mutate([{ title, category: categoryKey, due_date: due || null }], {
      onSuccess: () => { setTitle(""); setDue(""); },
    });
  };
  return (
    <form className="add-row" onSubmit={submit}>
      <input placeholder={synced ? "New work item (goes to triage)" : "New item"} value={title}
             onChange={(e) => setTitle(e.target.value)} required />
      <input type="date" value={due} onChange={(e) => setDue(e.target.value)} title="Deadline (defaults to 7 days)" />
      <button type="submit" disabled={create.isPending}>Add</button>
      <ErrorText error={create.error} />
    </form>
  );
}

function ItemRow({ item }: { item: Item }) {
  const done = useAction(api.done);
  const remove = useAction(api.remove);
  const confirmDelete = () => {
    const msg = item.redmine_issue_id
      ? `Delete "${item.title}"? Redmine #${item.redmine_issue_id} will be set to Rejected at the next push.`
      : `Delete "${item.title}"?`;
    if (confirm(msg)) remove.mutate([item.uuid]);
  };
  return (
    <li className="row">
      <div className="row-main">
        <Link to={`/items/${item.uuid}`} className="title">{item.title}</Link>
        <div className="meta">
          <DueLabel item={item} />
          <span className="pill">{item.status.replace("_", " ")}</span>
          {item.redmine_issue_id && <span className="muted">#{item.redmine_issue_id}</span>}
          {item.needs_triage && <span className="pill warn">needs triage</span>}
          {item.open_conflicts > 0 && <Link to="/conflicts" className="pill danger">{item.open_conflicts} PR</Link>}
          {item.dirty && <span className="pill" title="Changes waiting for the next push">unpushed</span>}
        </div>
      </div>
      <div className="row-actions">
        <button onClick={() => done.mutate([item.uuid])} disabled={done.isPending}>Done</button>
        <button className="ghost danger" onClick={confirmDelete} disabled={remove.isPending}>Delete</button>
      </div>
      <ErrorText error={done.error ?? remove.error} />
    </li>
  );
}
