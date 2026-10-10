import type { MetricId } from '../config/metrics';
import type { WidgetProps } from './registry';
import { MetricValue, resolveMetric, WidgetFrame } from './shared';

export interface KpiGroupOptions {
  metrics: MetricId[];
}

export function KpiGroupWidget({ data, envelope, instance, lang }: WidgetProps<KpiGroupOptions>) {
  return (
    <WidgetFrame instance={instance} lang={lang}>
      <div className="kpi-row">
        {instance.options.metrics.map((id) => {
          const metric = resolveMetric(id, data, lang, envelope.currency);
          return (
            <div className="kpi" key={id} data-metric={id}>
              <div className="kpi-label">{metric.label}</div>
              <div className="kpi-value">
                <MetricValue metric={metric} lang={lang} />
              </div>
              {metric.hint ? <div className="kpi-hint">{metric.hint}</div> : null}
            </div>
          );
        })}
      </div>
    </WidgetFrame>
  );
}
