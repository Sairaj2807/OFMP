# Security and threat model

## Assets

1. **Broker credentials** (Angel One PIN, TOTP seed, API key) and session tokens. Whoever holds them
   can act on the trading account.
2. **User accounts and sessions.**
3. **Licensed market data.** Redistribution is restricted; see the data entitlements note below.
4. **Research IP.** The classifier and verified dataset (note: the repository is public).
5. **Availability during market hours.**

## Controls in place

| Area | Control |
|---|---|
| Secrets | Read from env / `.env*` files, never committed (`.gitignore`, `.dockerignore`, gitleaks in CI). Production refuses a weak `JWT_SECRET`. Broker credentials are read only server-side and never reach the browser. |
| Passwords | Argon2id, rehashed when parameters change. Minimum strength rules. Equal work for unknown emails. |
| Sessions | 15-minute access JWT in an HttpOnly Lax cookie. 30-day opaque refresh token stored as SHA-256 only, in an HttpOnly Strict cookie scoped to `/api/v1/auth`. Rotation with reuse detection (reuse revokes the session). Server-side revocation is checked on every request and every 15 s on the stream. |
| CSRF | Double-submit token required on every cookie-authenticated unsafe request, plus SameSite cookies. |
| Authorization | Code-defined roles and permissions, checked server-side on every route and on the stream. Ownership checks (e.g. revoking a session). Switching the live contract needs `admin.system`. |
| Enumeration / brute force | Register and password reset return the same response for any email. Per-IP and per-account login limits (app), plus per-IP limits at nginx. |
| Transport | TLS 1.2/1.3 only, HSTS, HTTP→HTTPS redirect. |
| Browser | Strict CSP: `script-src` lists hashes of the shipped inline scripts, **no `'unsafe-inline'`**, `frame-ancestors 'none'`, `connect-src` limited to the page's own origin. Also nosniff, Referrer-Policy, Permissions-Policy and COOP. No HTML-string rendering in the UI (DOM `textContent` only). |
| WebSocket | Origin must match Host (blocks cross-site WebSocket hijacking). Auth required. Validated messages, 4 KB frame cap, per-user and global connection caps, constant-memory backpressure. |
| API | Versioned. Standard error envelope with no stack traces. 1 MB request cap (app and nginx). Unknown fields rejected. Request IDs end to end. |
| Containers | Non-root user; read-only root filesystem with tmpfs `/tmp`; `no-new-privileges`; all capabilities dropped. OS security updates applied at build time. Trivy gate on fixable HIGH/CRITICAL (both images scanned clean on 2026-09-30). |
| Network | Only nginx is published. Database and Redis sit on an internal-only network. Redis requires a password. Grafana binds to localhost. `/metrics` is not reachable from outside. |
| Supply chain | Pinned dependencies. Dependabot (pip, npm, actions, docker). `pip-audit` and `npm audit` in CI. |
| Audit | Security actions go to `audit_logs`: login, failures, logout, token reuse, password changes, session revocation, user creation. |

## Threats and residual risks

| Threat | Mitigation | Residual risk / next step |
|---|---|---|
| Stolen `.env.production` | file permissions 600; never in git or images | Move secrets to a secret manager; encrypt broker credentials at rest. |
| XSS stealing sessions | HttpOnly cookies, strict CSP, no HTML injection | — |
| Account takeover by guessing | Argon2id, rate limits, generic errors | Add 2FA / passkeys. |
| Stolen refresh token | rotation with reuse detection revokes the session | Device-bound tokens (future). |
| CSRF | double-submit token plus SameSite | — |
| Cross-site WebSocket hijacking | Origin check | — |
| Data scraping / abuse | auth required, per-IP limits, connection caps | Entitlement quotas per plan; historical range limits once history APIs exist. |
| Rate limits across processes | limits are in-process (the API is single-process today) | Move limiters to Redis before running more than one API process. |
| DoS on the stream | connection caps, backpressure, nginx limits | Upstream DDoS protection (CDN/WAF). |
| Supply-chain compromise | pinned deps, audits, Trivy, Dependabot | Pin GitHub Actions to commit SHAs; sign images. |
| Public repository | no secrets committed (verified) | The research IP is public by the owner's choice. |
| Log leakage | secret keys redacted in structured logs; TOTP codes are not printed | The dev email sender logs links; it refuses to run in production. |

## Data entitlements

Exchange data from Angel One is licensed to the account holder. Before exposing live or historical
data to other users, confirm redistribution rights with the provider or exchange.

The permission model (`market.read`) is the hook for a future per-user entitlement service.

## Reporting

Report vulnerabilities privately to the repository owner. Do not open public issues for them.
