import { createContext, useContext } from 'react';
import { statsClient } from '../api';
import type { StatsClient } from '../api/client';

/** Lets tests (and later the app) inject a different client, e.g. one with a fake fetch. */
export const ApiContext = createContext<StatsClient>(statsClient);

export function useStatsClient(): StatsClient {
  return useContext(ApiContext);
}
