import type { MetricId } from '../config/metrics';
import type { WidgetProps } from './registry';
import { MetricValue, resolveMetric, WidgetFrame } from './shared';

export interface KpiOptions {
  metric: MetricId;
}

export function KpiWidget({ data, instance, lang }: WidgetProps<KpiOptions>) {
  const metric = resolveMetric(instance.options.metric, data, lang);
  return (
    <WidgetFrame instance={instance} lang={lang}>
      <div className="kpi">
        <div className="kpi-label">{metric.label}</div>
        <div className="kpi-value">
          <MetricValue metric={metric} lang={lang} />
        </div>
        {metric.hint ? <div className="kpi-hint">{metric.hint}</div> : null}
      </div>
    </WidgetFrame>
  );
}
