"use client";

import { useEffect } from "react";

import { Button, Kbd, StatusDot, type DotState } from "@/components/ui/controls";
import { istWeekdayTime } from "@/lib/format";
import type { StreamStatus } from "@/lib/stream";
import type { MarketSession } from "@/lib/types";
import { useSession } from "@/stores/session";
import { useTerminal, type Layout } from "@/stores/terminal";
import { AlertsMenu } from "./AlertsMenu";
import { WorkspaceMenu } from "./WorkspaceMenu";

function connection(stream: StreamStatus, feedConnected: boolean, feedError: string | null): { state: DotState; label: string } {
  if (stream === "open") {
    return feedConnected ? { state: "ok", label: "Live" } : { state: "warn", label: feedError ? "Feed error" : "Feed idle" };
  }
  if (stream === "connecting" || stream === "reconnecting") return { state: "warn", label: "Reconnecting…" };
  if (stream === "unauthenticated") return { state: "down", label: "Signed out" };
  if (stream === "forbidden") return { state: "down", label: "No access" };
  return { state: "down", label: "Offline" };
}

function marketLabel(m: MarketSession | null): string | null {
  if (!m || m.open) return null;
  const reason = m.holiday ? `Market closed: ${m.holiday}` : "Market closed";
  return m.next_open ? `${reason} · opens ${istWeekdayTime(m.next_open)} IST` : reason;
}

export function TopBar({ onOpenPalette, onLogout }: { onOpenPalette: () => void; onLogout: () => void }) {
  const user = useSession((s) => s.user);
  const stream = useSession((s) => s.stream);
  const feed = useSession((s) => s.feed);
  const layout = useTerminal((s) => s.layout);
  const setLayout = useTerminal((s) => s.setLayout);
  const units = useTerminal((s) => s.units);
  const setUnits = useTerminal((s) => s.setUnits);
  const conn = connection(stream, feed.connected, feed.error);
  const closed = marketLabel(useSession((s) => s.market));
  const notice = useSession((s) => s.notice);
  const setNotice = useSession((s) => s.setNotice);
  useEffect(() => {
    if (!notice) return;
    const t = setTimeout(() => setNotice(null), 8000);
    return () => clearTimeout(t);
  }, [notice, setNotice]);

  return (
    <header className="flex h-10 shrink-0 items-center gap-3 border-b border-line bg-panel px-3">
      <span className="font-semibold tracking-wide text-fg">OFMP</span>
      <span className="h-4 w-px bg-line" aria-hidden />
      <span className="num text-[13px] text-fg" aria-label="Active contract">{feed.symbol ?? "—"}</span>
      <span title={feed.error ?? undefined}><StatusDot state={conn.state} label={conn.label} /></span>
      {closed && <span className="text-[12px] text-muted" aria-label="Market session">{closed}</span>}

      {notice && (
        <div role="status" className="flex items-center gap-2 rounded border border-warn/40 bg-warn/10 px-2 py-0.5 text-[12px] text-warn">
          {notice}
          <button type="button" aria-label="Dismiss" onClick={() => setNotice(null)} className="text-warn/80 hover:text-warn">×</button>
        </div>
      )}
      <div className="ml-auto flex items-center gap-1" role="group" aria-label="Chart layout">
        {([1, 2, 4] as Layout[]).map((n) => (
          <Button key={n} active={layout === n} onClick={() => setLayout(n)} aria-label={`${n} chart${n > 1 ? "s" : ""}`}>
            {n === 1 ? "▣" : n === 2 ? "◫" : "⊞"}
          </Button>
        ))}
      </div>
      <div className="flex items-center gap-1" role="group" aria-label="Quantity units">
        <Button active={units === "qty"} onClick={() => setUnits("qty")}>Qty</Button>
        <Button active={units === "lots"} onClick={() => setUnits("lots")} title={`1 lot = ${feed.lotSize}`}>Lots</Button>
      </div>
      <WorkspaceMenu />
      <AlertsMenu />
      <button
        type="button"
        onClick={onOpenPalette}
        className="flex h-6 items-center gap-2 rounded border border-line bg-panel-2 px-2 text-[12px] text-muted hover:border-line-strong"
      >
        Commands <Kbd>Ctrl K</Kbd>
      </button>
      <span className="h-4 w-px bg-line" aria-hidden />
      <span className="text-[12px] text-fg-2" title={user?.role}>{user?.display_name ?? user?.email}</span>
      <Button onClick={onLogout}>Sign out</Button>
    </header>
  );
}
