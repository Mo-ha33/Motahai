import { describe, expect, it } from 'vitest';
import { getMetric, METRIC_IDS, type MetricId } from './metrics';
import { emptySummaryData, summaryEnvelope } from '../test/fixtures/summary';

const d = summaryEnvelope.data;

describe('metrics', () => {
  const expected: Record<MetricId, number | null> = {
    orders_placed: 200,
    orders_cod: 150,
    orders_confirmed: 120,
    confirmation_rate: 0.6,
    cod_share: 0.75,
    cohort_orders: 100,
    cohort_delivered: 60,
    delivery_rate: 0.6,
    ad_orders: 40,
    ad_delivered: 30,
    ad_delivery_rate: 0.75,
    delivered_value: 3000,
    gross_value: 4260,
    ad_delivered_value: 1500,
    value_inflation: 1.42,
    refused_count: 9,
    refused_value: 450.5,
    signal_events: 400,
    confirmed_sent: 80,
    confirmed_shadow: 40,
    delivered_sent: 50,
    delivered_shadow: 10,
    flagged_events: 20,
    flagged_share: 0.05,
    late_delivery: 3,
  };

  it.each(METRIC_IDS)('%s selects the right value', (id) => {
    expect(getMetric(id).select(d)).toBeCloseTo(expected[id] as number, 10);
  });

  it('returns null for ratios with a zero denominator, never 0 or NaN', () => {
    for (const id of [
      'confirmation_rate',
      'cod_share',
      'delivery_rate',
      'ad_delivery_rate',
      'value_inflation',
      'flagged_share',
    ] as const) {
      expect(getMetric(id).select(emptySummaryData)).toBeNull();
    }
  });

  it('keeps plain counts at 0 on empty data', () => {
    expect(getMetric('orders_placed').select(emptySummaryData)).toBe(0);
  });

  it('getMetric throws on an unknown id', () => {
    expect(() => getMetric('nope')).toThrow(/Unknown metric/);
  });
});
