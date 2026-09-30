// Shared e2e settings (a plain module: test files may not import each other).
export const EMAIL = process.env.E2E_EMAIL ?? "demo@example.com";
export const PASSWORD = process.env.E2E_PASSWORD ?? "demo password 1";
export const AUTH_FILE = "e2e/.auth/user.json";
