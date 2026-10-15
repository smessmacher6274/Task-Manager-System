// Typed client for the FastAPI server (eco/main.py). Cookie-based session auth.

export interface Me { id: number; name: string; email: string; has_redmine: boolean; dry_run: boolean }
export interface Category { key: string; id: number; name: string; kind: "work" | "personal" | "custom"; db: string; syncs_redmine: boolean }
export interface Item {
  uuid: string; title: string; description: string; status: string; priority: string;
  due_date: string | null; deadline_origin: "redmine" | "self"; source: string;
  category_id: number; category_key: string; category: string | null; kind: string | null;
  captured_at: string; updated_at: string; deleted_at: string | null;
  redmine_issue_id: number | null; redmine_project_id: number | null; tracker_id: number | null;
  needs_triage: boolean; dirty: boolean; open_conflicts: number;
}
export interface Conflict {
  id: number; item_uuid: string; item_title: string; redmine_issue_id: number | null; field: string;
  base_value: string | null; server_value: string | null; redmine_value: string | null;
  redmine_journal_id: number | null; redmine_author: string | null; status: string; detected_at: string;
}
export interface ThreadEntry {
  kind: "note" | "journal"; id: number; author: string; body: string; private: boolean; at: string;
  pushed: boolean; error: string | null; redmine_journal_id: number | null;
}
export interface TimeEntry {
  id: number; hours: number; spent_on: string; activity_id: number; comment: string;
  pushed: boolean; error: string | null; redmine_time_entry_id: number | null;
}
export interface DeadlineEvent {
  id: number; event: "set" | "extended" | "met" | "missed"; old_due: string | null; new_due: string | null;
  origin: string; reason: string | null; ts: string;
}
export interface OutboxRow {
  id: number; item_uuid: string; item_title: string; payload: Record<string, unknown>;
  created_at: string; pushed_at: string | null; error: string | null; state: "pending" | "pushed" | "error";
}
export interface OutboxState {
  dry_run: boolean; last_poll_at: string | null; last_push_at: string | null; rows: OutboxRow[];
  pending_notes: { id: number; item_title: string; body: string; error: string | null }[];
  pending_hours: { id: number; item_title: string; hours: number; error: string | null }[];
}
export interface Lookups {
  projects: { id: number; name: string }[];
  trackers: { id: number; name: string }[];
  activities: { id: number; name: string }[];
}
export interface PushResult { dry_run: boolean; payloads: unknown[]; sent: number; errors: { error: string }[] }

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method,
    credentials: "same-origin",
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const j = await res.json();
      msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch { /* not json */ }
    throw new ApiError(res.status, msg);
  }
  return res.json() as Promise<T>;
}

export const api = {
  login: (email: string, password: string) => req<Me>("POST", "/auth/login", { email, password }),
  logout: () => req<{ ok: boolean }>("POST", "/auth/logout"),
  me: () => req<Me>("GET", "/auth/me"),
  categories: () => req<Category[]>("GET", "/categories"),
  items: (category?: string) => req<Item[]>("GET", `/items${category ? `?category=${encodeURIComponent(category)}` : ""}`),
  item: (uuid: string) => req<Item>("GET", `/items/${uuid}`),
  createItem: (b: { title: string; category: string; due_date?: string | null; description?: string }) =>
    req<Item>("POST", "/items", b),
  patchItem: (uuid: string, b: Partial<Pick<Item, "title" | "description" | "status" | "priority" | "due_date">> & { reason?: string }) =>
    req<Item>("PATCH", `/items/${uuid}`, b),
  done: (uuid: string) => req<Item>("POST", `/items/${uuid}/done`),
  remove: (uuid: string) => req<Item>("DELETE", `/items/${uuid}`),
  triageQueue: () => req<Item[]>("GET", "/triage"),
  triage: (uuid: string, redmine_project_id: number, tracker_id: number) =>
    req<Item>("POST", `/items/${uuid}/triage`, { redmine_project_id, tracker_id }),
  thread: (uuid: string) => req<ThreadEntry[]>("GET", `/items/${uuid}/thread`),
  addNote: (uuid: string, body: string, priv = false) => req<{ id: number }>("POST", `/items/${uuid}/notes`, { body, private: priv }),
  timeEntries: (uuid: string) => req<TimeEntry[]>("GET", `/items/${uuid}/time_entries`),
  addTime: (uuid: string, b: { hours: number; spent_on: string; activity_id: number; comment: string }) =>
    req<{ id: number }>("POST", `/items/${uuid}/time_entries`, b),
  deadlineEvents: (uuid: string) => req<DeadlineEvent[]>("GET", `/items/${uuid}/deadline_events`),
  conflicts: () => req<Conflict[]>("GET", "/conflicts"),
  resolve: (id: number, choice: "keep_server" | "take_redmine", reason?: string) =>
    req<{ id: number; status: string }>("POST", `/conflicts/${id}/resolve`, { choice, reason }),
  outbox: () => req<OutboxState>("GET", "/outbox"),
  poll: () => req<unknown>("POST", "/sync/poll"),
  push: () => req<PushResult>("POST", "/sync/push"),
  lookups: () => req<Lookups>("GET", "/lookups"),
};
