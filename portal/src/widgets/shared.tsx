import type { ReactNode } from 'react';
import type { StatsSummaryData } from '../api/types';
import { getMetric, type MetricDef, type MetricId } from '../config/metrics';
import type { WidgetInstance } from '../config/dashboard';
import { formatMetric, NO_VALUE } from '../format';
import { dictionaries, type Lang } from '../i18n';

/** Frame shared by every widget: optional title plus content. */
export function WidgetFrame({
  instance,
  lang,
  children,
}: {
  instance: WidgetInstance<unknown>;
  lang: Lang;
  children: ReactNode;
}) {
  const t = dictionaries[lang];
  return (
    <section className="widget" data-widget={instance.type}>
      {instance.titleKey ? <h2 className="widget-title">{t.widgetTitles[instance.titleKey]}</h2> : null}
      {children}
    </section>
  );
}

export interface ResolvedMetric {
  def: MetricDef;
  label: string;
  hint?: string;
  value: number | null;
  text: string;
}

export function resolveMetric(id: MetricId, data: StatsSummaryData, lang: Lang): ResolvedMetric {
  const t = dictionaries[lang];
  const def = getMetric(id);
  const value = def.select(data);
  return {
    def,
    label: t.metrics[def.labelKey],
    hint: def.hintKey ? t.metricHints[def.hintKey] : undefined,
    value,
    text: formatMetric(value, def.format, lang),
  };
}

/** A formatted value; undefined ratios show an em dash with a "no data" title. */
export function MetricValue({ metric, lang }: { metric: ResolvedMetric; lang: Lang }) {
  const title = metric.text === NO_VALUE ? dictionaries[lang].noData : undefined;
  return (
    <span className="metric-value" title={title}>
      {metric.text}
    </span>
  );
}
