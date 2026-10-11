import type { StatsEnvelope } from '../../api/types';

export const summaryEnvelope: StatsEnvelope = {
  tenant_id: 7,
  window: { start: '2026-10-03T00:00:00+00:00', end: '2026-10-10T00:00:00+00:00' },
  cohort_window: { start: '2026-09-26T00:00:00+00:00', end: '2026-10-03T00:00:00+00:00' },
  data: {
    'week-orders': { placed: 200, cod: 150, confirmed: 120 },
    'cohort-delivery': {
      all: { orders: 100, delivered: 60, delivered_value: 3000, gross_value: 4260 },
      ad_driven: { orders: 40, delivered: 30, delivered_value: 1500, gross_value: 1800 },
    },
    'refused-cod': { count: 9, value: 450.5 },
    'signal-health': {
      confirmed_sent: 80,
      confirmed_shadow: 40,
      delivered_sent: 50,
      delivered_shadow: 10,
      total_events: 400,
      flagged_events: 20,
      late_delivery: 3,
    },
    creatives: [
      { ad_id: 'ad-111', orders: 25, delivered: 20, delivery_rate: 0.8, delivered_value: 1000 },
      { ad_id: 'ad-222', orders: 15, delivered: 10, delivery_rate: 0.6667, delivered_value: 500 },
    ],
  },
};

export const emptySummaryData = {
  'week-orders': { placed: 0, cod: 0, confirmed: 0 },
  'cohort-delivery': {
    all: { orders: 0, delivered: 0, delivered_value: 0, gross_value: 0 },
    ad_driven: { orders: 0, delivered: 0, delivered_value: 0, gross_value: 0 },
  },
  'refused-cod': { count: 0, value: 0 },
  'signal-health': {
    confirmed_sent: 0,
    confirmed_shadow: 0,
    delivered_sent: 0,
    delivered_shadow: 0,
    total_events: 0,
    flagged_events: 0,
    late_delivery: 0,
  },
  creatives: [],
} satisfies StatsEnvelope['data'];
