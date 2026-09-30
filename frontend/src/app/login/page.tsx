"use client";

import { useRouter } from "next/navigation";
import { type FormEvent, useEffect, useState } from "react";

import { ApiError, api } from "@/lib/api";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // An existing session (or a still-valid refresh cookie) goes straight to the terminal.
  useEffect(() => {
    api.me().then(() => router.replace("/")).catch(() => undefined);
  }, [router]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.login(email, password);
      router.replace("/");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not reach the server.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="grid h-full place-items-center p-4">
      <form onSubmit={submit} className="w-full max-w-sm rounded-md border border-line bg-panel p-7" aria-labelledby="login-title">
        <h1 id="login-title" className="text-[17px] font-semibold text-fg">Sign in</h1>
        <p className="mb-5 text-muted">OFMP order-flow terminal</p>
        <label htmlFor="email" className="mb-1 block text-[12px] text-fg-2">Email</label>
        <input id="email" type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)}
               className="mb-4 h-9 w-full rounded border border-line bg-bg px-2.5 text-fg" autoFocus />
        <label htmlFor="password" className="mb-1 block text-[12px] text-fg-2">Password</label>
        <input id="password" type="password" autoComplete="current-password" required value={password}
               onChange={(e) => setPassword(e.target.value)} className="h-9 w-full rounded border border-line bg-bg px-2.5 text-fg" />
        <button type="submit" disabled={busy}
                className="mt-5 h-9 w-full rounded bg-accent font-semibold text-white disabled:opacity-60">
          {busy ? "Signing in…" : "Sign in"}
        </button>
        <p className="mt-3 min-h-5 text-[12px] text-sell" role="alert">{error ? `Error: ${error}` : ""}</p>
        <p className="text-[12px] text-muted">
          New here or forgot your password? Use the <a className="text-accent hover:underline" href="/login">account page</a>.
        </p>
      </form>
    </main>
  );
}
