// Small shared controls with consistent sizing, focus and disabled states.
import type { ButtonHTMLAttributes, ReactNode, SelectHTMLAttributes } from "react";

export function Button({ active, className = "", ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { active?: boolean }) {
  return (
    <button
      type="button"
      aria-pressed={active}
      className={`h-6 rounded px-2 text-[12px] transition-colors disabled:opacity-40 ${
        active ? "bg-accent/20 text-fg ring-1 ring-accent/60" : "text-fg-2 hover:bg-raised hover:text-fg"
      } ${className}`}
      {...props}
    />
  );
}

export function Select({ label, children, className = "", ...props }: SelectHTMLAttributes<HTMLSelectElement> & { label: string }) {
  return (
    <label className={`flex items-center gap-1 text-[12px] text-muted ${className}`}>
      <span className="sr-only">{label}</span>
      <select
        aria-label={label}
        className="h-6 rounded border border-line bg-panel-2 px-1.5 text-fg-2 hover:border-line-strong"
        {...props}
      >
        {children}
      </select>
    </label>
  );
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="rounded border border-line-strong bg-panel-2 px-1 text-[11px] text-fg-2">{children}</kbd>;
}

export type DotState = "ok" | "warn" | "down";

/** Status indicator: colour plus a text label (never colour alone). */
export function StatusDot({ state, label }: { state: DotState; label: string }) {
  const color = { ok: "bg-buy", warn: "bg-warn", down: "bg-sell" }[state];
  return (
    <span className="inline-flex items-center gap-1.5 text-[12px] text-fg-2" role="status">
      <span className={`h-2 w-2 rounded-full ${color}`} aria-hidden />
      {label}
    </span>
  );
}
