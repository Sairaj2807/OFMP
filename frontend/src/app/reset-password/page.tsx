"use client";

import { type FormEvent, useState } from "react";

import { AuthCard, type AuthMessage, Field, Message, Submit, useUrlToken } from "@/components/auth/AuthCard";
import { ApiError, api } from "@/lib/api";

/** Target of the password-reset email link: /app/reset-password/?token=... */
export default function ResetPasswordPage() {
  const token = useUrlToken();
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [msg, setMsg] = useState<AuthMessage>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);

  const missing: AuthMessage = token === null
    ? { kind: "error", text: "This link is missing its token. Request a new one from the sign-in page." } : null;

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (password !== confirm) {
      setMsg({ kind: "error", text: "Passwords do not match." });
      return;
    }
    setBusy(true);
    setMsg(null);
    try {
      await api.confirmPasswordReset(token!, password);
      setDone(true);
      setMsg({ kind: "ok", text: "Password updated. You can sign in now." });
    } catch (err) {
      setMsg({ kind: "error", text: err instanceof ApiError ? err.message : "Could not reach the server." });
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthCard title="Choose a new password">
      {!done && token && (
        <form onSubmit={submit} aria-label="Choose a new password">
          <Field id="password" label="New password" type="password" autoComplete="new-password" required autoFocus
                 value={password} onChange={(e) => setPassword(e.target.value)} />
          <Field id="confirm" label="Confirm new password" type="password" autoComplete="new-password" required
                 value={confirm} onChange={(e) => setConfirm(e.target.value)} />
          <Submit busy={busy}>{busy ? "Saving…" : "Set password"}</Submit>
        </form>
      )}
      <Message msg={missing ?? msg} />
      <p className="text-[12px]"><a className="text-accent hover:underline" href="/app/login/">Back to sign in</a></p>
    </AuthCard>
  );
}
