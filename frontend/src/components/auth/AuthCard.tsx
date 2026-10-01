import { type InputHTMLAttributes, type ReactNode, useSyncExternalStore } from "react";

/** Centered card shared by the sign-in, reset-password and verify-email pages. */
export function AuthCard({ title, subtitle, children }: { title: string; subtitle?: string; children: ReactNode }) {
  return (
    <main className="grid h-full place-items-center p-4">
      <section className="w-full max-w-sm rounded-md border border-line bg-panel p-7" aria-labelledby="auth-title">
        <h1 id="auth-title" className="text-[17px] font-semibold text-fg">{title}</h1>
        <p className="mb-5 text-muted">{subtitle ?? "OFMP order-flow terminal"}</p>
        {children}
      </section>
    </main>
  );
}

export function Field({ id, label, ...props }: { id: string; label: string } & InputHTMLAttributes<HTMLInputElement>) {
  return (
    <>
      <label htmlFor={id} className="mb-1 block text-[12px] text-fg-2">{label}</label>
      <input id={id} className="mb-4 h-9 w-full rounded border border-line bg-bg px-2.5 text-fg" {...props} />
    </>
  );
}

export function Submit({ busy, children }: { busy: boolean; children: ReactNode }) {
  return (
    <button type="submit" disabled={busy}
            className="mt-1 h-9 w-full rounded bg-accent font-semibold text-white disabled:opacity-60">
      {children}
    </button>
  );
}

export type AuthMessage = { kind: "ok" | "error"; text: string } | null;

/** Success or error line under a form (announced to screen readers). */
export function Message({ msg }: { msg: AuthMessage }) {
  if (!msg) return <p className="mt-3 min-h-5" />;
  return msg.kind === "error"
    ? <p role="alert" className="mt-3 min-h-5 text-[12px] text-sell">Error: {msg.text}</p>
    : <p role="status" className="mt-3 min-h-5 text-[12px] text-buy">{msg.text}</p>;
}

const noSubscribe = () => () => undefined;

/** The `token` query parameter. The pages are statically exported, so it is
 *  read on the client only (undefined while prerendering / before hydration). */
export function useUrlToken(): string | null | undefined {
  return useSyncExternalStore(noSubscribe, () => new URLSearchParams(window.location.search).get("token"),
                              () => undefined);
}
