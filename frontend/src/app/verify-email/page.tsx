"use client";

import { useEffect, useState } from "react";

import { AuthCard, type AuthMessage, Message, useUrlToken } from "@/components/auth/AuthCard";
import { ApiError, api } from "@/lib/api";

/** Target of the verification email link: /app/verify-email/?token=... */
export default function VerifyEmailPage() {
  const token = useUrlToken();
  const [result, setResult] = useState<AuthMessage>(null);

  useEffect(() => {
    if (!token) return;
    api.verifyEmail(token)
      .then(() => setResult({ kind: "ok", text: "Your email address is verified." }))
      .catch((err) => setResult({ kind: "error", text: err instanceof ApiError ? err.message : "Could not reach the server." }));
  }, [token]);
  const msg: AuthMessage = token === null ? { kind: "error", text: "This link is missing its token." } : result;

  return (
    <AuthCard title="Email verification">
      {msg ? <Message msg={msg} /> : <p role="status" className="text-muted">Verifying…</p>}
      <p className="text-[12px]"><a className="text-accent hover:underline" href="/app/login/">Continue to sign in</a></p>
    </AuthCard>
  );
}
