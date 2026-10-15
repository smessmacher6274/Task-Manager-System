import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { ConflictCard } from "./pages/Conflicts";
import { TriageRow } from "./pages/Triage";
import type { Conflict, Item } from "./api";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

const wrap = (ui: ReactNode) =>
  render(<QueryClientProvider client={new QueryClient()}><MemoryRouter>{ui}</MemoryRouter></QueryClientProvider>);

const okFetch = () => vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("{}", { status: 200 }));

const conflict: Conflict = {
  id: 7, item_uuid: "u1", item_title: "Fix login", redmine_issue_id: 42, field: "title",
  base_value: "A", server_value: "Mine", redmine_value: "Theirs", redmine_journal_id: 3,
  redmine_author: "Co Worker", status: "pending", detected_at: "2026-10-07T12:00:00Z",
};

describe("ConflictCard", () => {
  it("shows base, server and Redmine values", () => {
    wrap(<ConflictCard c={conflict} />);
    expect(screen.getByText("Mine")).toBeTruthy();
    expect(screen.getByText("Theirs")).toBeTruthy();
    expect(screen.getByText("Redmine (Co Worker)")).toBeTruthy();
  });

  it("requires a reason to keep the server value", async () => {
    const f = okFetch();
    wrap(<ConflictCard c={conflict} />);
    const keep = screen.getByText("Keep server") as HTMLButtonElement;
    expect(keep.disabled).toBe(true);
    fireEvent.change(screen.getByPlaceholderText(/Reason/), { target: { value: "agreed" } });
    expect(keep.disabled).toBe(false);
    fireEvent.click(keep);
    await waitFor(() => expect(f).toHaveBeenCalledWith("/api/conflicts/7/resolve", expect.objectContaining({
      method: "POST", body: JSON.stringify({ choice: "keep_server", reason: "agreed" }),
    })));
  });

  it("takes Redmine without a reason", async () => {
    const f = okFetch();
    wrap(<ConflictCard c={conflict} />);
    fireEvent.click(screen.getByText("Take Redmine"));
    await waitFor(() => expect(f).toHaveBeenCalledWith("/api/conflicts/7/resolve", expect.objectContaining({
      body: JSON.stringify({ choice: "take_redmine" }),
    })));
  });
});

describe("TriageRow", () => {
  const item = {
    uuid: "t1", title: "From tablet", due_date: "2026-10-14", deadline_origin: "self", source: "tablet",
    captured_at: "2026-10-07T12:00:00Z", needs_triage: true,
  } as Item;
  const lookups = { projects: [{ id: 1, name: "Eco Test" }], trackers: [{ id: 2, name: "Task" }], activities: [] };

  it("saves only once project and tracker are chosen", async () => {
    const f = okFetch();
    wrap(<TriageRow item={item} lookups={lookups} />);
    const save = screen.getByText("Save") as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Project"), { target: { value: "1" } });
    fireEvent.change(screen.getByLabelText("Tracker"), { target: { value: "2" } });
    fireEvent.click(save);
    await waitFor(() => expect(f).toHaveBeenCalledWith("/api/items/t1/triage", expect.objectContaining({
      body: JSON.stringify({ redmine_project_id: 1, tracker_id: 2 }),
    })));
  });
});
