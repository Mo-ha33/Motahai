import type { MetricFormat } from '../format';
import type { Dictionary } from '../i18n';
import type { StatsSummaryData } from '../api/types';

export const METRIC_IDS = [
  'orders_placed',
  'orders_cod',
  'orders_confirmed',
  'confirmation_rate',
  'cod_share',
  'cohort_orders',
  'cohort_delivered',
  'delivery_rate',
  'ad_orders',
  'ad_delivered',
  'ad_delivery_rate',
  'delivered_value',
  'gross_value',
  'ad_delivered_value',
  'value_inflation',
  'refused_count',
  'refused_value',
  'signal_events',
  'confirmed_sent',
  'confirmed_shadow',
  'delivered_sent',
  'delivered_shadow',
  'flagged_events',
  'flagged_share',
  'late_delivery',
] as const;
export type MetricId = (typeof METRIC_IDS)[number];

export interface MetricDef {
  id: MetricId;
  labelKey: keyof Dictionary['metrics'];
  hintKey?: keyof Dictionary['metricHints'];
  format: MetricFormat;
  select(s: StatsSummaryData): number | null;
}

/** Ratios are undefined (null) when the denominator is 0. */
function ratio(numerator: number, denominator: number): number | null {
  return denominator === 0 ? null : numerator / denominator;
}

const week = (s: StatsSummaryData) => s['week-orders'];
const cohort = (s: StatsSummaryData) => s['cohort-delivery'];
const signal = (s: StatsSummaryData) => s['signal-health'];
const refused = (s: StatsSummaryData) => s['refused-cod'];

const defs: MetricDef[] = [
  { id: 'orders_placed', labelKey: 'orders_placed', format: 'count', select: (s) => week(s).placed },
  { id: 'orders_cod', labelKey: 'orders_cod', format: 'count', select: (s) => week(s).cod },
  { id: 'orders_confirmed', labelKey: 'orders_confirmed', format: 'count', select: (s) => week(s).confirmed },
  {
    id: 'confirmation_rate',
    labelKey: 'confirmation_rate',
    hintKey: 'confirmation_rate',
    format: 'percent',
    select: (s) => ratio(week(s).confirmed, week(s).placed),
  },
  {
    id: 'cod_share',
    labelKey: 'cod_share',
    hintKey: 'cod_share',
    format: 'percent',
    select: (s) => ratio(week(s).cod, week(s).placed),
  },
  {
    id: 'cohort_orders',
    labelKey: 'cohort_orders',
    hintKey: 'cohort_orders',
    format: 'count',
    select: (s) => cohort(s).all.orders,
  },
  { id: 'cohort_delivered', labelKey: 'cohort_delivered', format: 'count', select: (s) => cohort(s).all.delivered },
  {
    id: 'delivery_rate',
    labelKey: 'delivery_rate',
    hintKey: 'delivery_rate',
    format: 'percent',
    select: (s) => ratio(cohort(s).all.delivered, cohort(s).all.orders),
  },
  { id: 'ad_orders', labelKey: 'ad_orders', format: 'count', select: (s) => cohort(s).ad_driven.orders },
  { id: 'ad_delivered', labelKey: 'ad_delivered', format: 'count', select: (s) => cohort(s).ad_driven.delivered },
  {
    id: 'ad_delivery_rate',
    labelKey: 'ad_delivery_rate',
    hintKey: 'ad_delivery_rate',
    format: 'percent',
    select: (s) => ratio(cohort(s).ad_driven.delivered, cohort(s).ad_driven.orders),
  },
  { id: 'delivered_value', labelKey: 'delivered_value', format: 'money', select: (s) => cohort(s).all.delivered_value },
  { id: 'gross_value', labelKey: 'gross_value', format: 'money', select: (s) => cohort(s).all.gross_value },
  {
    id: 'ad_delivered_value',
    labelKey: 'ad_delivered_value',
    format: 'money',
    select: (s) => cohort(s).ad_driven.delivered_value,
  },
  {
    id: 'value_inflation',
    labelKey: 'value_inflation',
    hintKey: 'value_inflation',
    format: 'ratio',
    select: (s) => ratio(cohort(s).all.gross_value, cohort(s).all.delivered_value),
  },
  { id: 'refused_count', labelKey: 'refused_count', hintKey: 'refused_count', format: 'count', select: (s) => refused(s).count },
  { id: 'refused_value', labelKey: 'refused_value', format: 'money', select: (s) => refused(s).value },
  { id: 'signal_events', labelKey: 'signal_events', format: 'count', select: (s) => signal(s).total_events },
  { id: 'confirmed_sent', labelKey: 'confirmed_sent', format: 'count', select: (s) => signal(s).confirmed_sent },
  { id: 'confirmed_shadow', labelKey: 'confirmed_shadow', format: 'count', select: (s) => signal(s).confirmed_shadow },
  { id: 'delivered_sent', labelKey: 'delivered_sent', format: 'count', select: (s) => signal(s).delivered_sent },
  { id: 'delivered_shadow', labelKey: 'delivered_shadow', format: 'count', select: (s) => signal(s).delivered_shadow },
  { id: 'flagged_events', labelKey: 'flagged_events', format: 'count', select: (s) => signal(s).flagged_events },
  {
    id: 'flagged_share',
    labelKey: 'flagged_share',
    hintKey: 'flagged_share',
    format: 'percent',
    select: (s) => ratio(signal(s).flagged_events, signal(s).total_events),
  },
  { id: 'late_delivery', labelKey: 'late_delivery', hintKey: 'late_delivery', format: 'count', select: (s) => signal(s).late_delivery },
];

const byId = new Map<string, MetricDef>(defs.map((d) => [d.id, d]));

export const metrics: readonly MetricDef[] = defs;

/** Loud on unknown ids: a config typo must fail in dev and tests. */
export function getMetric(id: string): MetricDef {
  const def = byId.get(id);
  if (!def) throw new Error(`Unknown metric id: ${id}`);
  return def;
}
