/** Exact shapes of the Stats API (src/ameen_workforce/stats_routes.py). */

export const STATS_SECTIONS = [
  'week-orders',
  'cohort-delivery',
  'refused-cod',
  'signal-health',
  'creatives',
] as const;
export type StatsSection = (typeof STATS_SECTIONS)[number];

/** Date window sent to the API: ISO dates (YYYY-MM-DD), half-open [start, end), UTC. */
export interface StatsWindow {
  start: string;
  end: string;
}

export interface WeekOrders {
  placed: number;
  cod: number;
  confirmed: number;
}

export interface CohortBucket {
  orders: number;
  delivered: number;
  delivered_value: number;
  gross_value: number;
}

export interface CohortDelivery {
  all: CohortBucket;
  ad_driven: CohortBucket;
}

export interface RefusedCod {
  count: number;
  value: number;
}

export interface SignalHealth {
  confirmed_sent: number;
  confirmed_shadow: number;
  delivered_sent: number;
  delivered_shadow: number;
  total_events: number;
  flagged_events: number;
  late_delivery: number;
}

export interface CreativeRow {
  ad_id: string;
  orders: number;
  delivered: number;
  delivery_rate: number;
  delivered_value: number;
}

export interface SectionDataMap {
  'week-orders': WeekOrders;
  'cohort-delivery': CohortDelivery;
  'refused-cod': RefusedCod;
  'signal-health': SignalHealth;
  creatives: CreativeRow[];
}

/** `summary.data`: every section keyed by its endpoint name. */
export type StatsSummaryData = SectionDataMap;

export interface StatsEnvelope<D = StatsSummaryData> {
  tenant_id: number;
  /** Report window as full ISO datetimes. */
  window: StatsWindow;
  /** Present on cohort-based sections and on summary (window shifted back 7 days). */
  cohort_window?: StatsWindow;
  data: D;
}
