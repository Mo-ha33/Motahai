/**
 * Pluggable auth. No key, token or env var is ever read in browser code:
 * in operator mode a server-side proxy adds the operator key; in tenant mode the signed-in tenant key is supplied
 * by the session (see auth/session.ts), held in sessionStorage only.
 */
export interface AuthProvider {
  headers(): Promise<Record<string, string>> | Record<string, string>;
}

/** Same-origin requests; a server-side proxy adds credentials. */
export const sameOriginAuth: AuthProvider = {
  headers: () => ({}),
};

/** Bearer auth from a caller-supplied token source (the session's tenant key). */
export function bearerAuth(getToken: () => string | null): AuthProvider {
  return {
    headers: (): Record<string, string> => {
      const token = getToken();
      return token ? { Authorization: `Bearer ${token}` } : {};
    },
  };
}
