"use client";

import { useRouter } from "next/navigation";
import { type FormEvent, useEffect, useState } from "react";

import { AuthCard, type AuthMessage, Field, Message, Submit } from "@/components/auth/AuthCard";
import { ApiError, api } from "@/lib/api";

type Mode = "signin" | "register" | "forgot";

const TITLES: Record<Mode, string> = { signin: "Sign in", register: "Create account", forgot: "Reset password" };
const SUBMIT: Record<Mode, string> = { signin: "Sign in", register: "Create account", forgot: "Send reset link" };

export default function LoginPage() {
  const router = useRouter();
  const [mode, setMode] = useState<Mode>("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [msg, setMsg] = useState<AuthMessage>(null);
  const [busy, setBusy] = useState(false);

  // An existing session (or a still-valid refresh cookie) goes straight to the terminal.
  useEffect(() => {
    api.me().then(() => router.replace("/")).catch(() => undefined);
  }, [router]);

  const switchTo = (m: Mode) => {
    setMode(m);
    setMsg(null);
  };

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setMsg(null);
    try {
      if (mode === "signin") {
        await api.login(email, password);
        router.replace("/");
      } else if (mode === "register") {
        const r = await api.register(email, password, name.trim() || null);
        setMode("signin");
        setPassword("");
        setMsg({ kind: "ok", text: r.message });
      } else {
        setMsg({ kind: "ok", text: (await api.requestPasswordReset(email)).message });
      }
    } catch (err) {
      setMsg({ kind: "error", text: err instanceof ApiError ? err.message : "Could not reach the server." });
    } finally {
      setBusy(false);
    }
  }

  const link = "text-accent hover:underline";
  return (
    <AuthCard title={TITLES[mode]}>
      <form onSubmit={submit} aria-label={TITLES[mode]}>
        {mode === "register" && (
          <Field id="name" label="Display name (optional)" autoComplete="name" maxLength={100} value={name}
                 onChange={(e) => setName(e.target.value)} />
        )}
        <Field id="email" label="Email" type="email" autoComplete="username" required autoFocus value={email}
               onChange={(e) => setEmail(e.target.value)} />
        {mode !== "forgot" && (
          <Field id="password" label="Password" type="password" required value={password}
                 autoComplete={mode === "register" ? "new-password" : "current-password"}
                 onChange={(e) => setPassword(e.target.value)} />
        )}
        <Submit busy={busy}>{busy ? "Please wait…" : SUBMIT[mode]}</Submit>
        <Message msg={msg} />
      </form>
      <p className="flex justify-between text-[12px] text-muted">
        {mode === "signin" ? (
          <>
            <button type="button" className={link} onClick={() => switchTo("forgot")}>Forgot password?</button>
            <button type="button" className={link} onClick={() => switchTo("register")}>Create account</button>
          </>
        ) : (
          <button type="button" className={link} onClick={() => switchTo("signin")}>Back to sign in</button>
        )}
      </p>
    </AuthCard>
  );
}
