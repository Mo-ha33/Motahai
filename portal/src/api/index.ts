import { createStatsClient } from './client';

/** The one app-wide client. Requests are same-origin; credentials come from the server-side proxy. */
export const statsClient = createStatsClient();

export * from './auth';
export * from './client';
export * from './onboarding';
export * from './types';
