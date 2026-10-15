import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { Item } from "./api";

export const fmtDate = (iso: string | null) =>
  iso ? new Date(iso.length === 10 ? iso + "T00:00:00" : iso).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" }) : "—";

export const fmtTime = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : "—");

export const today = () => new Date().toLocaleDateString("en-CA"); // YYYY-MM-DD local

export function DueLabel({ item }: { item: Pick<Item, "due_date" | "deadline_origin"> }) {
  if (!item.due_date) return <span className="muted">no deadline</span>;
  const overdue = item.due_date < today();
  return (
    <span className={overdue ? "due overdue" : "due"} title={`deadline from ${item.deadline_origin}`}>
      {fmtDate(item.due_date)}
      {item.deadline_origin === "redmine" && <span className="origin">RM</span>}
    </span>
  );
}

export function ErrorText({ error }: { error: unknown }) {
  if (!error) return null;
  return <p className="error">{error instanceof Error ? error.message : String(error)}</p>;
}

/** Mutation that refreshes every query when it settles (small app, cheap). */
export function useAction<A extends unknown[], R>(fn: (...a: A) => Promise<R>) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (args: A) => fn(...args),
    onSettled: () => qc.invalidateQueries(),
  });
}
