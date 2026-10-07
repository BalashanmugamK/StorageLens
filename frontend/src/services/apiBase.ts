// Shared API base location, extracted so the auth module can live
// under services/ without an import cycle with api.ts (auth adds the
// Authorization header api.ts needs; neither owns the base URL).

export const API_BASE_URL = (
  import.meta.env.VITE_API_BASE_URL ?? ""
).replace(/\/+$/, "");

export function isConfigured(): boolean {
  return API_BASE_URL.length > 0;
}