"use client";

import { useEffect, useRef, useState } from "react";

import { type SaveStatus, useWorkspaceStore, workspaceActions } from "@/hooks/useWorkspaces";
import { ApiError } from "@/lib/api";

const STATUS_LABEL: Record<SaveStatus, string> = { idle: "", saving: "Saving…", saved: "Saved", error: "Not saved" };

type Pending = { kind: "saveAs" | "rename" | "duplicate"; value: string } | null;

/** Current workspace, with switch / save as / rename / duplicate / default / delete. */
export function WorkspaceMenu() {
  const { available, list, current, status } = useWorkspaceStore();
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState<Pending>(null);
  const [error, setError] = useState<string | null>(null);
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => !root.current?.contains(e.target as Node) && setOpen(false);
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  if (!available) return null;

  const run = async (fn: () => Promise<void>) => {
    setError(null);
    try {
      await fn();
      setPending(null);
      setOpen(false);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Something went wrong");
    }
  };

  const submit = () => {
    if (!pending || !pending.value.trim()) return;
    const name = pending.value.trim();
    void run(() => (pending.kind === "saveAs" ? workspaceActions.saveAs(name)
      : pending.kind === "rename" ? workspaceActions.rename(name) : workspaceActions.duplicate(name)));
  };

  return (
    <div className="relative" ref={root}>
      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => { setOpen((o) => !o); setPending(null); setError(null); }}
        className="flex h-6 items-center gap-2 rounded border border-line bg-panel-2 px-2 text-[12px] text-fg-2 hover:border-line-strong"
      >
        <span className="text-muted">Workspace</span>
        <span className="max-w-40 truncate text-fg">{current?.name ?? "—"}</span>
        <span className={`text-[11px] ${status === "error" ? "text-sell" : "text-muted"}`}>{STATUS_LABEL[status]}</span>
      </button>
      {open && (
        <div role="menu" aria-label="Workspaces"
             className="absolute right-0 top-8 z-40 w-72 rounded-md border border-line-strong bg-panel p-1 shadow-2xl">
          <div className="px-2 py-1 text-[10px] uppercase tracking-wider text-muted">Switch to</div>
          <ul className="max-h-60 overflow-y-auto">
            {list.map((w) => (
              <li key={w.id}>
                <button type="button" role="menuitemradio" aria-checked={w.id === current?.id}
                        onClick={() => void run(() => workspaceActions.switchTo(w.id))}
                        className={`flex w-full items-center justify-between rounded px-2 py-1 text-left ${
                          w.id === current?.id ? "bg-raised text-fg" : "text-fg-2 hover:bg-raised"}`}>
                  <span className="truncate">{w.name}</span>
                  {w.is_default && <span className="text-[10px] uppercase text-muted">default</span>}
                </button>
              </li>
            ))}
          </ul>
          <div className="my-1 h-px bg-line" />
          {pending ? (
            <form onSubmit={(e) => { e.preventDefault(); submit(); }} className="flex gap-1 p-1">
              <label className="sr-only" htmlFor="ws-name">Workspace name</label>
              <input id="ws-name" autoFocus maxLength={80} value={pending.value}
                     onChange={(e) => setPending({ ...pending, value: e.target.value })}
                     className="h-7 flex-1 rounded border border-line bg-bg px-2 text-fg" />
              <button type="submit" className="h-7 rounded bg-accent px-2 text-[12px] font-semibold text-white">
                {pending.kind === "rename" ? "Rename" : "Save"}
              </button>
            </form>
          ) : (
            <div className="grid grid-cols-2 gap-1 p-1 text-[12px]">
              {[
                ["Save as…", () => setPending({ kind: "saveAs", value: "" })],
                ["Rename…", () => setPending({ kind: "rename", value: current?.name ?? "" })],
                ["Duplicate…", () => setPending({ kind: "duplicate", value: `${current?.name ?? "Workspace"} (copy)` })],
                ["Make default", () => void run(workspaceActions.makeDefault)],
              ].map(([label, fn]) => (
                <button key={label as string} type="button" role="menuitem" onClick={fn as () => void}
                        className="rounded px-2 py-1 text-left text-fg-2 hover:bg-raised hover:text-fg">{label as string}</button>
              ))}
              <button type="button" role="menuitem" disabled={list.length <= 1}
                      onClick={() => void run(workspaceActions.remove)}
                      className="col-span-2 rounded px-2 py-1 text-left text-sell hover:bg-sell-bg disabled:opacity-40">
                Delete this workspace
              </button>
            </div>
          )}
          {error && <p role="alert" className="px-2 pb-1 text-[12px] text-sell">Error: {error}</p>}
        </div>
      )}
    </div>
  );
}
