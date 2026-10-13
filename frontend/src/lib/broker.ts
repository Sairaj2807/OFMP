// The daily broker session (Angel One login before each NSE session) as one
// short top-bar label. The server sends traders the state and the next step;
// admins also get the last error and attempts (shown in the tooltip).
import { istTime, istWeekdayTime } from "./format";
import type { BrokerStatus } from "./types";

export type BrokerTone = "ok" | "warn" | "down" | "muted";

export function brokerLabel(b: BrokerStatus | null): { text: string; tone: BrokerTone; title: string } | null {
  if (!b) return null;                                   // no scheduled session (synthetic feed, worker mode)
  const at = (iso: string | null | undefined) => (iso ? istTime(new Date(iso).getTime() / 1000) : "—");
  const next = b.next_action_at ? ` · next login ${istWeekdayTime(b.next_action_at)} IST` : "";
  const detail = [b.last_error && `Last error: ${b.last_error}`, b.attempts ? `Attempts today: ${b.attempts}` : null,
                  b.token_expires_at && `Token valid until ${istWeekdayTime(b.token_expires_at)} IST`,
                  b.login_paused_until && `Automatic logins paused until ${istTime(b.login_paused_until)}`]
    .filter(Boolean).join("\n");
  switch (b.state) {
    case "ready":
      return { text: `Broker: logged in ${at(b.logged_in_at)}`, tone: "ok",
               title: [`Feed on until ${b.next_action_at ? istTime(new Date(b.next_action_at).getTime() / 1000) : "the close"}`,
                       detail].filter(Boolean).join("\n") };
    case "logging_in":
      return { text: "Broker: logging in…", tone: "warn", title: detail };
    case "failed":
      return { text: "Broker: login failed", tone: "down",
               title: detail || "The morning login failed; an admin can use Re-login." };
    case "stopped":
      return { text: `Broker: feed off${next}`, tone: "muted", title: "The feed is off after the close." };
    case "waiting":
      return { text: `Broker: waiting${next}`, tone: "muted", title: detail };
    default:
      return { text: "Broker: starting…", tone: "muted", title: detail };
  }
}
