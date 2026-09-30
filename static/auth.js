// Shared helpers for the account pages (login, reset password, verify email).
// Talks to /api/v1/auth; session tokens live in HttpOnly cookies, never in JS.

function csrfToken() {
  const m = document.cookie.match(/(?:^|;\s*)ofmp_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

async function api(path, body) {
  const res = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = null;
  try { data = await res.json(); } catch (_) { /* 204 */ }
  return { ok: res.ok, status: res.status, data };
}

function errorText(result) {
  const err = result.data && result.data.error;
  if (!err) return "Something went wrong (" + result.status + ").";
  if (err.details && err.details.length) return err.message + ": " + err.details.map((d) => d.field + " " + d.message).join(", ");
  return err.message;
}

// Only same-site relative paths: never redirect to another origin.
function safeNext() {
  const next = new URLSearchParams(location.search).get("next") || "/chart";
  return /^\/(?!\/)[\w\-./?=&%]*$/.test(next) ? next : "/chart";
}

function setMessage(el, text, kind) {
  el.textContent = text;
  el.dataset.kind = kind || "";
}
