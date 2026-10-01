"use client";

import { useEffect, useRef, useState } from "react";

import { type AlertTab, alertActions, useAlertStore } from "@/hooks/useAlerts";
import { KIND_INFO, KIND_ORDER, type RuleDraft, draftToInput } from "@/lib/alerts";
import { ApiError } from "@/lib/api";
import { intervalLabel, istDateTime } from "@/lib/format";
import { snapshotBus } from "@/lib/snapshots";
import type { AlertRule } from "@/lib/types";

const field = "h-7 rounded border border-line bg-bg px-2 text-[12px] text-fg";
const small = "rounded px-2 py-0.5 text-[12px] text-fg-2 hover:bg-raised hover:text-fg disabled:opacity-40";

function errorText(e: unknown) {
  return e instanceof ApiError ? e.message : "Something went wrong";
}

/** Bell with the unread count; opens the alerts panel (history, rules, webhooks). */
export function AlertsMenu() {
  const { available, open, tab, unread, set } = useAlertStore();
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => !root.current?.contains(e.target as Node) && set({ open: false });
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && set({ open: false });
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, set]);

  if (!available) return null;

  return (
    <div className="relative" ref={root}>
      <button
        type="button"
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label={unread ? `Alerts, ${unread} unread` : "Alerts"}
        onClick={() => set({ open: !open })}
        className="flex h-6 items-center gap-1.5 rounded border border-line bg-panel-2 px-2 text-[12px] text-fg-2 hover:border-line-strong"
      >
        <svg viewBox="0 0 16 16" className="h-3.5 w-3.5" aria-hidden fill="none" stroke="currentColor" strokeWidth="1.5">
          <path d="M4 11V7a4 4 0 0 1 8 0v4l1 1.5H3L4 11ZM6.5 14a1.5 1.5 0 0 0 3 0" strokeLinejoin="round" />
        </svg>
        Alerts
        {unread > 0 && (
          <span className="num rounded-full bg-accent px-1.5 text-[10px] font-semibold leading-4 text-white">
            {unread > 99 ? "99+" : unread}
          </span>
        )}
      </button>
      {open && (
        <div role="dialog" aria-label="Alerts"
             className="absolute right-0 top-8 z-40 flex max-h-[70vh] w-[28rem] flex-col rounded-md border border-line-strong bg-panel shadow-2xl">
          <div role="tablist" aria-label="Alerts sections" className="flex gap-1 border-b border-line p-1">
            {(["history", "rules", "webhooks"] as AlertTab[]).map((t) => (
              <button key={t} type="button" role="tab" aria-selected={tab === t} onClick={() => set({ tab: t })}
                      className={`rounded px-2 py-1 text-[12px] capitalize ${tab === t ? "bg-raised text-fg" : "text-fg-2 hover:bg-raised"}`}>
                {t}
              </button>
            ))}
          </div>
          <div role="tabpanel" aria-label={tab} className="min-h-0 flex-1 overflow-y-auto p-2">
            {tab === "history" && <History />}
            {tab === "rules" && <Rules />}
            {tab === "webhooks" && <Webhooks />}
          </div>
        </div>
      )}
    </div>
  );
}

function History() {
  const { events, unread } = useAlertStore();
  const [error, setError] = useState<string | null>(null);
  const [permission, setPermission] = useState(() =>
    typeof Notification === "undefined" ? "unsupported" : Notification.permission);

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between text-[12px] text-muted">
        <span>{unread ? `${unread} unread` : "All read"}</span>
        <div className="flex gap-1">
          {permission === "default" && (
            <button type="button" className={small}
                    onClick={async () => setPermission(await alertActions.requestBrowserNotifications())}>
              Desktop notifications
            </button>
          )}
          <button type="button" className={small} disabled={!unread}
                  onClick={() => alertActions.markAllRead().catch((e) => setError(errorText(e)))}>
            Mark all read
          </button>
        </div>
      </div>
      {error && <p role="alert" className="text-[12px] text-sell">Error: {error}</p>}
      {events.length === 0 ? (
        <p className="py-6 text-center text-[12px] text-muted">No alerts yet. Create a rule under Rules.</p>
      ) : (
        <ul aria-label="Alert history" className="flex flex-col gap-1">
          {events.map((e) => (
            <li key={e.id} className={`rounded border px-2 py-1.5 ${e.read_at ? "border-line" : "border-accent/50 bg-accent/5"}`}>
              <div className="flex items-baseline justify-between gap-2">
                <span className="truncate text-[12px] text-fg">{e.message}</span>
                <span className="num shrink-0 text-[11px] text-muted">{istDateTime(e.fired_at)}</span>
              </div>
              <div className="flex gap-2 text-[11px] text-muted">
                {e.value != null && <span className="num">value {e.value}</span>}
                {e.symbol && <span>{e.symbol}</span>}
                {e.suppressed && <span className="text-warn">not delivered: {e.suppressed.replace("_", " ")}</span>}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function lastPrice(): string {
  for (const id of ["c1", "c2", "c3", "c4"]) {
    const ltp = snapshotBus.get(id)?.quote?.ltp;
    if (ltp != null) return String(ltp);
  }
  return "";
}

function Rules() {
  const { rules, kinds } = useAlertStore();
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = (p: Promise<unknown>) => p.catch((e) => setError(errorText(e)));
  const atLimit = kinds ? rules.length >= kinds.max_rules : false;

  return (
    <div className="flex flex-col gap-2">
      {creating ? (
        <RuleForm onDone={() => setCreating(false)} />
      ) : (
        <div className="flex items-center justify-between">
          <span className="text-[12px] text-muted">
            Evaluated on live data for the active contract{kinds ? ` · ${rules.length}/${kinds.max_rules}` : ""}
          </span>
          <button type="button" disabled={atLimit} onClick={() => setCreating(true)}
                  className="h-7 rounded bg-accent px-2 text-[12px] font-semibold text-white disabled:opacity-40">
            New alert
          </button>
        </div>
      )}
      {error && <p role="alert" className="text-[12px] text-sell">Error: {error}</p>}
      {rules.length === 0 && !creating && (
        <p className="py-6 text-center text-[12px] text-muted">No alert rules.</p>
      )}
      <ul aria-label="Alert rules" className="flex flex-col gap-1">
        {rules.map((r) => <RuleRow key={r.id} rule={r} run={run} />)}
      </ul>
    </div>
  );
}

function RuleRow({ rule, run }: { rule: AlertRule; run: (p: Promise<unknown>) => void }) {
  return (
    <li className="flex items-center gap-2 rounded border border-line px-2 py-1.5" aria-label={`Rule ${rule.name}`}>
      <label className="flex items-center" title={rule.enabled ? "Pause" : "Resume"}>
        <input type="checkbox" checked={rule.enabled} aria-label={`${rule.name} enabled`}
               onChange={(e) => run(alertActions.setEnabled(rule, e.target.checked))} />
      </label>
      <div className="min-w-0 flex-1">
        <div className={`truncate text-[12px] ${rule.enabled ? "text-fg" : "text-muted"}`}>{rule.name}</div>
        <div className="truncate text-[11px] text-muted">
          {rule.description} · {rule.mode === "once" ? "once" : `every ${rule.cooldown_sec}s at most`}
          {rule.channel_ids.length > 0 && ` · ${rule.channel_ids.length} webhook${rule.channel_ids.length > 1 ? "s" : ""}`}
          {rule.fire_count > 0 && ` · fired ${rule.fire_count}×`}
        </div>
      </div>
      <button type="button" className={`${small} text-sell hover:bg-sell-bg`} aria-label={`Delete ${rule.name}`}
              onClick={() => run(alertActions.deleteRule(rule.id))}>
        Delete
      </button>
    </li>
  );
}

function RuleForm({ onDone }: { onDone: () => void }) {
  const { kinds, channels } = useAlertStore();
  const intervals = kinds?.intervals ?? [60, 300, 900];
  const [draft, setDraft] = useState<RuleDraft>(() => ({
    name: "", kind: "price_above", level: lastPrice(), interval: intervals[0], side: "any", mode: "repeat",
    cooldownSec: kinds?.cooldown_sec.default ?? 300, channelIds: [],
  }));
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const info = KIND_INFO[draft.kind];
  const update = (patch: Partial<RuleDraft>) => setDraft((d) => ({ ...d, ...patch }));

  const submit = async () => {
    const input = draftToInput(draft);
    if (typeof input === "string") return setError(input);
    setBusy(true);
    setError(null);
    try {
      await alertActions.createRule(input);
      onDone();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form aria-label="New alert" onSubmit={(e) => { e.preventDefault(); void submit(); }}
          className="flex flex-col gap-2 rounded border border-line-strong p-2">
      <label className="flex flex-col gap-0.5 text-[11px] text-muted">
        Name
        <input className={field} maxLength={80} placeholder={info.label} value={draft.name}
               onChange={(e) => update({ name: e.target.value })} />
      </label>
      <div className="grid grid-cols-2 gap-2">
        <label className="flex flex-col gap-0.5 text-[11px] text-muted">
          Condition
          <select className={field} value={draft.kind}
                  onChange={(e) => {
                    const kind = e.target.value as RuleDraft["kind"];
                    // a price makes no sense as a delta/volume level and vice versa: prefill or clear
                    const wasPrice = info.level === "price", isPrice = KIND_INFO[kind].level === "price";
                    const level = isPrice ? (wasPrice ? draft.level : lastPrice()) : (wasPrice ? "" : draft.level);
                    update({ kind, side: "any", level });
                  }}>
            {KIND_ORDER.map((k) => <option key={k} value={k}>{KIND_INFO[k].label}</option>)}
          </select>
        </label>
        {info.level && (
          <label className="flex flex-col gap-0.5 text-[11px] text-muted">
            Level
            <input className={`${field} num`} inputMode="decimal" value={draft.level}
                   onChange={(e) => update({ level: e.target.value })} />
          </label>
        )}
        {info.candle && (
          <label className="flex flex-col gap-0.5 text-[11px] text-muted">
            Candle
            <select className={field} value={draft.interval} onChange={(e) => update({ interval: Number(e.target.value) })}>
              {intervals.map((s) => <option key={s} value={s}>{intervalLabel(s)}</option>)}
            </select>
          </label>
        )}
        {info.sides && (
          <label className="flex flex-col gap-0.5 text-[11px] text-muted">
            Side
            <select className={field} value={draft.side} onChange={(e) => update({ side: e.target.value })}>
              {info.sides.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
            </select>
          </label>
        )}
        <label className="flex flex-col gap-0.5 text-[11px] text-muted">
          Repeat
          <select className={field} value={draft.mode} onChange={(e) => update({ mode: e.target.value as RuleDraft["mode"] })}>
            <option value="repeat">Every time</option>
            <option value="once">Once, then pause</option>
          </select>
        </label>
        {draft.mode === "repeat" && (
          <label className="flex flex-col gap-0.5 text-[11px] text-muted">
            At most every (seconds)
            <input className={`${field} num`} type="number" min={kinds?.cooldown_sec.min ?? 10}
                   max={kinds?.cooldown_sec.max ?? 86400} value={draft.cooldownSec}
                   onChange={(e) => update({ cooldownSec: Number(e.target.value) })} />
          </label>
        )}
      </div>
      {channels.length > 0 && (
        <fieldset className="flex flex-wrap gap-3 text-[12px] text-fg-2">
          <legend className="mb-0.5 text-[11px] text-muted">Also send to</legend>
          {channels.map((c) => (
            <label key={c.id} className="flex items-center gap-1">
              <input type="checkbox" checked={draft.channelIds.includes(c.id)}
                     onChange={(e) => update({ channelIds: e.target.checked
                       ? [...draft.channelIds, c.id] : draft.channelIds.filter((x) => x !== c.id) })} />
              {c.name}
            </label>
          ))}
        </fieldset>
      )}
      {info.candle && (
        <p className="text-[11px] text-muted">Checked when the candle closes, i.e. on the first trade of the next one.</p>
      )}
      {error && <p role="alert" className="text-[12px] text-sell">Error: {error}</p>}
      <div className="flex justify-end gap-1">
        <button type="button" className={small} onClick={onDone}>Cancel</button>
        <button type="submit" disabled={busy}
                className="h-7 rounded bg-accent px-3 text-[12px] font-semibold text-white disabled:opacity-40">
          Create alert
        </button>
      </div>
    </form>
  );
}

function Webhooks() {
  const { channels, kinds } = useAlertStore();
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [secret, setSecret] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const atLimit = kinds ? channels.length >= kinds.max_channels : false;

  const add = async () => {
    setError(null);
    setMessage(null);
    try {
      setSecret(await alertActions.createChannel(name.trim() || "Webhook", url.trim()));
      setName("");
      setUrl("");
    } catch (e) {
      setError(errorText(e));
    }
  };

  return (
    <div className="flex flex-col gap-2">
      <p className="text-[11px] text-muted">
        Alerts are POSTed as JSON, signed with HMAC-SHA256: verify <code>X-OFMP-Signature</code> over
        {" "}<code>{"<X-OFMP-Timestamp>.<body>"}</code> with the channel secret.
      </p>
      {secret && (
        <div role="status" className="rounded border border-warn/40 bg-warn/10 p-2 text-[12px] text-warn">
          Signing secret, shown only once. Copy it now:
          <code className="mt-1 block break-all text-fg" aria-label="Signing secret">{secret}</code>
          <button type="button" className={`${small} mt-1`} onClick={() => setSecret(null)}>I have copied it</button>
        </div>
      )}
      <ul aria-label="Webhooks" className="flex flex-col gap-1">
        {channels.map((c) => (
          <li key={c.id} className="flex items-center gap-2 rounded border border-line px-2 py-1.5">
            <div className="min-w-0 flex-1">
              <div className="truncate text-[12px] text-fg">{c.name}</div>
              <div className="truncate text-[11px] text-muted" title={c.url ?? undefined}>
                {c.url}
                {c.last_status && (
                  <span className={c.last_status === "ok" ? "text-buy" : "text-sell"}>
                    {" "}· last delivery {c.last_status}{c.last_error ? ` (${c.last_error})` : ""}
                  </span>
                )}
              </div>
            </div>
            <button type="button" className={small}
                    onClick={async () => {
                      setError(null);
                      try {
                        const r = await alertActions.testChannel(c.id);
                        setMessage(r.ok ? `Test delivered to ${c.name}` : `Test failed: ${r.error}`);
                      } catch (e) {
                        setError(errorText(e));
                      }
                    }}>
              Test
            </button>
            <button type="button" className={`${small} text-sell hover:bg-sell-bg`} aria-label={`Delete ${c.name}`}
                    onClick={() => alertActions.deleteChannel(c.id).catch((e) => setError(errorText(e)))}>
              Delete
            </button>
          </li>
        ))}
      </ul>
      {!atLimit && (
        <form aria-label="Add webhook" onSubmit={(e) => { e.preventDefault(); void add(); }} className="flex flex-col gap-1">
          <div className="flex gap-1">
            <input className={`${field} w-28`} placeholder="Name" maxLength={80} aria-label="Webhook name"
                   value={name} onChange={(e) => setName(e.target.value)} />
            <input className={`${field} flex-1`} placeholder="https://…" aria-label="Webhook URL" required
                   value={url} onChange={(e) => setUrl(e.target.value)} />
            <button type="submit" className="h-7 rounded bg-accent px-2 text-[12px] font-semibold text-white">Add</button>
          </div>
        </form>
      )}
      {message && <p role="status" className="text-[12px] text-fg-2">{message}</p>}
      {error && <p role="alert" className="text-[12px] text-sell">Error: {error}</p>}
    </div>
  );
}
