import type { NextConfig } from "next";
import { PHASE_DEVELOPMENT_SERVER } from "next/constants";

/**
 * Production: a static export (`out/`) that the FastAPI app serves at /app —
 * same origin as /api/v1 and /ws/v1/stream, so session cookies and the
 * WebSocket need no cross-origin setup.
 *
 * Development (`next dev`): the dev server proxies /api and the health routes
 * to the backend (BACKEND_URL, default http://127.0.0.1:8010). Rewrites are
 * incompatible with `output: "export"`, hence the per-phase config.
 */
const BACKEND_URL = process.env.BACKEND_URL ?? "http://127.0.0.1:8010";

export default function config(phase: string): NextConfig {
  const base: NextConfig = {
    basePath: "/app",
    trailingSlash: true, // emits login/index.html, which the backend's static mount resolves
    reactStrictMode: true,
    poweredByHeader: false,
  };
  if (phase === PHASE_DEVELOPMENT_SERVER) {
    return {
      ...base,
      async rewrites() {
        return ["/api/:path*", "/health", "/ready", "/live"].map((source) => ({
          source,
          destination: `${BACKEND_URL}${source}`,
          basePath: false as const,
        }));
      },
    };
  }
  return { ...base, output: "export", images: { unoptimized: true } };
}
