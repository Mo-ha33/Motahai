import { createContext, useContext, useMemo, type ReactNode } from 'react';
import { statsClient } from '../api';
import { bearerAuth, sameOriginAuth } from '../api/auth';
import { createStatsClient, type StatsClient } from '../api/client';
import { createOnboardingClient, onboardingClient, type OnboardingClient } from '../api/onboarding';
import { useSession } from '../auth/SessionContext';

/** Lets tests inject a different client, e.g. one with a fake fetch. <ApiProvider> overrides it from the session. */
export const ApiContext = createContext<StatsClient>(statsClient);

export const OnboardingApiContext = createContext<OnboardingClient>(onboardingClient);

export function useOnboardingClient(): OnboardingClient {
  return useContext(OnboardingApiContext);
}

export function useStatsClient(): StatsClient {
  return useContext(ApiContext);
}

/**
 * Builds the app-wide client from the session: `bearerAuth(() => session.key)` in tenant mode, `sameOriginAuth`
 * (the dev proxy adds the operator key) in operator mode.
 */
export function ApiProvider({ children, fetchImpl }: { children: ReactNode; fetchImpl?: typeof fetch }) {
  const { session } = useSession();
  const key = session?.mode === 'tenant' ? session.key : null;
  const client = useMemo(
    () =>
      createStatsClient({
        auth: key === null ? sameOriginAuth : bearerAuth(() => key),
        fetchImpl,
      }),
    [key, fetchImpl],
  );
  // Onboarding is operator-only: it always uses the same-origin (proxy-keyed) auth, never a tenant key.
  const onboarding = useMemo(() => createOnboardingClient({ auth: sameOriginAuth, fetchImpl }), [fetchImpl]);
  return (
    <ApiContext.Provider value={client}>
      <OnboardingApiContext.Provider value={onboarding}>{children}</OnboardingApiContext.Provider>
    </ApiContext.Provider>
  );
}
