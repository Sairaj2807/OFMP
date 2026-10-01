"use client";

import { useEffect } from "react";

import { TOAST_MS, alertActions, useAlertStore } from "@/hooks/useAlerts";
import { istDateTime } from "@/lib/format";
import type { AlertEvent } from "@/lib/types";

/** Alerts as they fire (bottom right). Each dismisses itself after TOAST_MS. */
export function AlertToasts() {
  const toasts = useAlertStore((s) => s.toasts);
  return (
    <div aria-live="polite" aria-label="New alerts" className="pointer-events-none fixed bottom-10 right-3 z-50 flex w-80 flex-col gap-2">
      {toasts.map((t) => <Toast key={t.id} event={t} />)}
    </div>
  );
}

function Toast({ event }: { event: AlertEvent }) {
  useEffect(() => {
    const timer = setTimeout(() => alertActions.dismissToast(event.id), TOAST_MS);
    return () => clearTimeout(timer);
  }, [event.id]);

  return (
    <div role="status" className="pointer-events-auto rounded-md border border-accent/60 bg-panel p-2 shadow-2xl">
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex items-baseline justify-between gap-2">
            <span className="truncate text-[12px] font-semibold text-fg">{event.rule_name ?? "Alert"}</span>
            <span className="num shrink-0 text-[11px] text-muted">{istDateTime(event.fired_at)}</span>
          </div>
          <p className="text-[12px] text-fg-2">{event.message}</p>
        </div>
        <button type="button" aria-label="Dismiss alert" onClick={() => alertActions.dismissToast(event.id)}
                className="text-muted hover:text-fg">×</button>
      </div>
      <button type="button" onClick={() => useAlertStore.getState().set({ open: true, tab: "history" })}
              className="mt-1 text-[11px] text-accent hover:underline">
        Open alerts
      </button>
    </div>
  );
}
