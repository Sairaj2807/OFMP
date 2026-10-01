"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Group, Panel, Separator } from "react-resizable-panels";

import { useAlertStore, useAlerts } from "@/hooks/useAlerts";
import { useLiveStream } from "@/hooks/useLiveStream";
import { flushSave, useWorkspaces } from "@/hooks/useWorkspaces";
import { ApiError, api } from "@/lib/api";
import { intervalLabel } from "@/lib/format";
import { useSession } from "@/stores/session";
import { type Layout, useTerminal } from "@/stores/terminal";
import { AlertToasts } from "./AlertToasts";
import { BottomBar } from "./BottomBar";
import { ChartGrid } from "./ChartGrid";
import { type Command, CommandPalette } from "./CommandPalette";
import { OrderBook } from "./OrderBook";
import { TopBar } from "./TopBar";
import { Watchlist } from "./Watchlist";

/**
 * Terminal shell. Resolves the signed-in user first (redirecting to /login if
 * there is no session), then opens the live stream and renders:
 *
 *   top bar
 *   watchlist | charts (1/2/4) | order book
 *   bottom figures
 */
export function Terminal() {
  const router = useRouter();
  const user = useSession((s) => s.user);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.me()
      .then(async (me) => {
        if (cancelled) return;
        useSession.getState().setUser(me);
        const contracts = await api.contracts().catch(() => ({ data: [] }));
        if (!cancelled) useSession.getState().setContracts(contracts.data);
      })
      .catch((e) => {
        if (cancelled) return;
        if (e instanceof ApiError && (e.status === 401 || e.status === 503)) router.replace("/login/");
        else setLoadError(e instanceof Error ? e.message : "could not load your session");
      });
    return () => {
      cancelled = true;
    };
  }, [router]);

  useLiveStream(user !== null);
  useWorkspaces(user !== null);
  useAlerts(user !== null);

  const logout = useCallback(async () => {
    await flushSave().catch(() => undefined);   // keep the last layout change
    await api.logout().catch(() => undefined);
    useSession.getState().setUser(null);
    router.replace("/login/");
  }, [router]);

  const intervals = useSession((s) => s.intervals);
  const commands = useMemo<Command[]>(() => {
    const t = useTerminal.getState;
    const active = () => t().activeChartId;
    const toggle = (key: "showPoc" | "showValueArea" | "showImbalances") => () => {
      const c = t().charts.find((x) => x.id === active());
      if (c) t().updateChart(c.id, { [key]: !c[key] });
    };
    return [
      ...intervals.map((sec, i) => ({
        id: `interval-${sec}`, group: "Interval", label: `${intervalLabel(sec)} on the focused chart`,
        shortcut: i < 9 ? String(i + 1) : undefined, run: () => t().updateChart(active(), { interval: sec }),
      })),
      ...([1, 2, 4] as Layout[]).map((n) => ({
        id: `layout-${n}`, group: "Layout", label: `${n} chart${n > 1 ? "s" : ""}`, run: () => t().setLayout(n),
      })),
      ...[1, 2, 3, 4].map((n) => ({ id: `focus-${n}`, group: "Focus", label: `Chart c${n}`, run: () => t().setActiveChart(`c${n}`) })),
      { id: "poc", group: "Toggle", label: "Point of control", shortcut: "P", run: toggle("showPoc") },
      { id: "va", group: "Toggle", label: "Value area", shortcut: "V", run: toggle("showValueArea") },
      { id: "imb", group: "Toggle", label: "Stacked imbalances", shortcut: "I", run: toggle("showImbalances") },
      { id: "units-qty", group: "Units", label: "Raw quantity", run: () => t().setUnits("qty") },
      { id: "units-lots", group: "Units", label: "Lots", run: () => t().setUnits("lots") },
      { id: "alerts", group: "Alerts", label: "Open alerts", run: () => useAlertStore.getState().set({ open: true, tab: "history" }) },
      { id: "logout", group: "Account", label: "Sign out", run: logout },
    ];
  }, [intervals, logout]);

  // Global shortcuts (ignored while typing in a field).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((o) => !o);
        return;
      }
      const target = e.target as HTMLElement | null;
      if (paletteOpen || e.ctrlKey || e.metaKey || e.altKey || target?.closest("input, select, textarea")) return;
      const byShortcut = commands.find((c) => c.shortcut && c.shortcut.toLowerCase() === e.key.toLowerCase());
      if (byShortcut) {
        e.preventDefault();
        byShortcut.run();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [commands, paletteOpen]);

  if (loadError) {
    return <main className="grid h-full place-items-center text-sell" role="alert">Error: {loadError}</main>;
  }
  if (!user) {
    return <main className="grid h-full place-items-center text-muted">Loading…</main>;
  }

  return (
    <div className="flex h-full flex-col">
      <TopBar onOpenPalette={() => setPaletteOpen(true)} onLogout={logout} />
      <main className="min-h-0 flex-1">
        <Group orientation="horizontal" id="terminal-main">
          <Panel id="watchlist" defaultSize="16" minSize={160} collapsible collapsedSize={0}>
            <Watchlist />
          </Panel>
          <Separator className="w-px" />
          <Panel id="charts" minSize="40">
            <ChartGrid />
          </Panel>
          <Separator className="w-px" />
          <Panel id="orderbook" defaultSize="18" minSize={200} collapsible collapsedSize={0}>
            <OrderBook />
          </Panel>
        </Group>
      </main>
      <BottomBar />
      <AlertToasts />
      {paletteOpen && <CommandPalette onClose={() => setPaletteOpen(false)} commands={commands} />}
    </div>
  );
}
