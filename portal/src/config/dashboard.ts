import type { FilterId } from '../filters/ids';
import type { WidgetTitleKey } from '../i18n';
import type { Route } from '../router';
import type { MetricId } from './metrics';
import type { CreativeColumn } from '../widgets/creatives-table';

export type WidgetSpan = 3 | 4 | 6 | 8 | 12;

export interface WidgetInstance<O = unknown> {
  id: string;
  type: string;
  titleKey?: WidgetTitleKey;
  /** Columns out of 12 on wide screens; everything is full width below 720px. Default 12. */
  span?: WidgetSpan;
  options: O;
}

export interface PageConfig {
  route: Route;
  filters: FilterId[];
  widgets: WidgetInstance[];
}

export interface DashboardConfig {
  pages: Partial<Record<Route, PageConfig>>;
}

const group = (
  id: string,
  titleKey: WidgetTitleKey,
  metrics: MetricId[],
  span: WidgetSpan = 12,
): WidgetInstance<{ metrics: MetricId[] }> => ({ id, type: 'kpi-group', titleKey, span, options: { metrics } });

const bars = (
  id: string,
  titleKey: WidgetTitleKey,
  metrics: MetricId[],
  span: WidgetSpan = 6,
): WidgetInstance<{ metrics: MetricId[] }> => ({ id, type: 'compare-bars', titleKey, span, options: { metrics } });

const creativeColumns: CreativeColumn[] = [
  'ad_id',
  'orders',
  'delivered',
  'delivery_rate',
  'delivered_value',
];

export const defaultDashboard: DashboardConfig = {
  pages: {
    // M4-4 (#51)
    roas: {
      route: 'roas',
      filters: ['tenant', 'date-range'],
      widgets: [
        { id: 'window', type: 'window-note', titleKey: 'window', span: 12, options: {} },
        group('orders', 'orders', ['orders_placed', 'orders_cod', 'orders_confirmed', 'confirmation_rate']),
        {
          id: 'cohort-funnel',
          type: 'funnel',
          titleKey: 'cohortFunnel',
          span: 6,
          options: { steps: ['cohort_orders', 'cohort_delivered'] satisfies MetricId[] },
        },
        group('delivery-quality', 'deliveryQuality', ['delivery_rate', 'ad_delivery_rate', 'value_inflation'], 6),
        bars('value-gap', 'valueGap', ['gross_value', 'delivered_value'], 6),
        group('refused', 'refusedCod', ['refused_count', 'refused_value'], 6),
        {
          id: 'creatives',
          type: 'creatives-table',
          titleKey: 'creatives',
          span: 12,
          options: { columns: creativeColumns, limit: 20 },
        },
      ],
    },
    // M4-3 (#50): partial until the health score (#42) lands.
    signal: {
      route: 'signal',
      filters: ['tenant', 'date-range'],
      widgets: [
        { id: 'window', type: 'window-note', titleKey: 'window', span: 12, options: {} },
        group('signal-overview', 'signalOverview', ['signal_events', 'flagged_share', 'late_delivery']),
        bars('confirmed-events', 'confirmedEvents', ['confirmed_sent', 'confirmed_shadow']),
        bars('delivered-events', 'deliveredEvents', ['delivered_sent', 'delivered_shadow']),
      ],
    },
  },
};
