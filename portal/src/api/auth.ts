/**
 * Pluggable auth. No key, token or env var is ever read in browser code:
 * today a server-side proxy adds the operator key; later a per-tenant key (#43) is supplied by the app.
 */
export interface AuthProvider {
  headers(): Promise<Record<string, string>> | Record<string, string>;
}

/** Same-origin requests; a server-side proxy adds credentials. */
export const sameOriginAuth: AuthProvider = {
  headers: () => ({}),
};

/** Bearer auth from a caller-supplied token source (future per-tenant key). */
export function bearerAuth(getToken: () => string | null): AuthProvider {
  return {
    headers: (): Record<string, string> => {
      const token = getToken();
      return token ? { Authorization: `Bearer ${token}` } : {};
    },
  };
}
