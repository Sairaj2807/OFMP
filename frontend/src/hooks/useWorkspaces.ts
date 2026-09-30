"use client";

// Server-side workspaces: the terminal layout saved per user.
//
// - On sign-in: load the default workspace (or create one from the current
//   layout if the user has none) and apply it.
// - Autosave: layout changes are saved to the current workspace after a
//   short pause, with its revision. If another window saved first (409), the
//   newer server copy is adopted and the user is told, rather than silently
//   overwriting it.
// - Without a database (503) the terminal keeps working on local storage only.
import { useEffect } from "react";
import { create } from "zustand";

import { ApiError, api } from "@/lib/api";
import type { Workspace, WorkspaceSummary } from "@/lib/types";
import { useSession } from "@/stores/session";
import { STORAGE_VERSION, type TerminalConfig, migrateConfig, toConfig, useTerminal } from "@/stores/terminal";

export type SaveStatus = "idle" | "saving" | "saved" | "error";

interface WorkspaceState {
  available: boolean;
  list: WorkspaceSummary[];
  current: { id: string; name: string; revision: number } | null;
  status: SaveStatus;
  set: (patch: Partial<Omit<WorkspaceState, "set">>) => void;
}

export const useWorkspaceStore = create<WorkspaceState>()((set) => ({
  available: false,
  list: [],
  current: null,
  status: "idle",
  set: (patch) => set(patch),
}));

const AUTOSAVE_MS = 1500;
let applying = false;          // set while applying a server config, so it is not saved straight back
let saveTimer: ReturnType<typeof setTimeout> | null = null;
let lastSaved = "";

const serialize = (c: TerminalConfig) => JSON.stringify(toConfig(c));

// The workspace last used in this browser reopens on the next visit (falls
// back to the user's default). Stored best-effort: it may be unavailable.
const LAST_KEY = "ofmp.workspace.last";
const rememberLast = (id: string) => { try { localStorage.setItem(LAST_KEY, id); } catch { /* private mode */ } };
const recallLast = () => { try { return localStorage.getItem(LAST_KEY); } catch { return null; } };

function adopt(ws: Workspace) {
  applying = true;
  useTerminal.getState().applyConfig(migrateConfig(ws.config));
  applying = false;
  lastSaved = serialize(useTerminal.getState());
  useWorkspaceStore.getState().set({ current: { id: ws.id, name: ws.name, revision: ws.revision }, status: "saved" });
  rememberLast(ws.id);
}

async function refreshList() {
  useWorkspaceStore.getState().set({ list: await api.workspaces() });
}

/** Save now if there are unsaved changes (also used before switching). */
export async function flushSave(keepalive = false): Promise<void> {
  if (saveTimer) {
    clearTimeout(saveTimer);
    saveTimer = null;
  }
  const { current, set } = useWorkspaceStore.getState();
  const config = serialize(useTerminal.getState());
  if (!current || config === lastSaved) return;
  set({ status: "saving" });
  try {
    const ws = await api.updateWorkspace(current.id, current.revision,
      { config: JSON.parse(config), config_version: STORAGE_VERSION }, keepalive);
    lastSaved = config;
    set({ current: { id: ws.id, name: ws.name, revision: ws.revision }, status: "saved" });
  } catch (e) {
    if (e instanceof ApiError && e.code === "REVISION_CONFLICT") {
      adopt(await api.workspace(current.id));
      useSession.getState().setNotice(`"${current.name}" was changed in another window — loaded the newer version.`);
    } else {
      set({ status: "error" });
    }
  }
}

export const workspaceActions = {
  async switchTo(id: string) {
    await flushSave();
    adopt(await api.workspace(id));
  },
  async saveAs(name: string) {
    await flushSave();
    const ws = await api.createWorkspace(name, toConfig(useTerminal.getState()), STORAGE_VERSION);
    adopt(ws);
    await refreshList();
  },
  async rename(name: string) {
    const cur = useWorkspaceStore.getState().current;
    if (!cur) return;
    await flushSave();
    const ws = await api.updateWorkspace(cur.id, useWorkspaceStore.getState().current!.revision, { name });
    useWorkspaceStore.getState().set({ current: { id: ws.id, name: ws.name, revision: ws.revision } });
    await refreshList();
  },
  async duplicate(name: string) {
    const cur = useWorkspaceStore.getState().current;
    if (!cur) return;
    await flushSave();
    adopt(await api.duplicateWorkspace(cur.id, name));
    await refreshList();
  },
  async makeDefault() {
    const cur = useWorkspaceStore.getState().current;
    if (!cur) return;
    await flushSave();
    const ws = await api.updateWorkspace(cur.id, useWorkspaceStore.getState().current!.revision, { is_default: true });
    useWorkspaceStore.getState().set({ current: { id: ws.id, name: ws.name, revision: ws.revision } });
    await refreshList();
  },
  async remove() {
    const cur = useWorkspaceStore.getState().current;
    if (!cur) return;
    await api.deleteWorkspace(cur.id);
    const list = await api.workspaces();
    useWorkspaceStore.getState().set({ list, current: null });
    if (list.length) adopt(await api.workspace((list.find((w) => w.is_default) ?? list[0]).id));
  },
};

export function useWorkspaces(enabled: boolean): void {
  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    (async () => {
      let list: WorkspaceSummary[];
      try {
        list = await api.workspaces();
      } catch {
        useWorkspaceStore.getState().set({ available: false });   // no database: local storage only
        return;
      }
      if (cancelled) return;
      useWorkspaceStore.getState().set({ available: true, list });
      const last = recallLast();
      const pick = list.find((w) => w.id === last) ?? list.find((w) => w.is_default) ?? list[0];
      if (pick) adopt(await api.workspace(pick.id));
      else await workspaceActions.saveAs("My workspace");
    })().catch(() => useWorkspaceStore.getState().set({ status: "error" }));

    const unsubscribe = useTerminal.subscribe((state) => {
      if (applying || !useWorkspaceStore.getState().current) return;
      if (serialize(state) === lastSaved) return;
      if (saveTimer) clearTimeout(saveTimer);
      saveTimer = setTimeout(() => void flushSave(), AUTOSAVE_MS);
    });
    // leaving or reloading the page: save a pending change without waiting for the debounce
    const onHide = () => void flushSave(true);
    window.addEventListener("pagehide", onHide);
    return () => {
      cancelled = true;
      unsubscribe();
      window.removeEventListener("pagehide", onHide);
      void flushSave();
    };
  }, [enabled]);
}
