import type { MetricId } from '../config/metrics';
import type { WidgetProps } from './registry';
import { MetricValue, resolveMetric, WidgetFrame } from './shared';

export interface CompareBarsOptions {
  metrics: MetricId[];
}

/** Horizontal bars on one shared scale (the largest value fills the track). */
export function CompareBarsWidget({ data, instance, lang }: WidgetProps<CompareBarsOptions>) {
  const items = instance.options.metrics.map((id) => ({ id, metric: resolveMetric(id, data, lang) }));
  const max = Math.max(0, ...items.map((i) => i.metric.value ?? 0));
  return (
    <WidgetFrame instance={instance} lang={lang}>
      <ul className="bars">
        {items.map(({ id, metric }, index) => {
          const pct = max > 0 && metric.value !== null ? (metric.value / max) * 100 : 0;
          return (
            <li className="bar-row" key={id} data-metric={id}>
              <div className="bar-head">
                <span className="bar-label">{metric.label}</span>
                <MetricValue metric={metric} lang={lang} />
              </div>
              <div className="bar-track">
                <div
                  className="bar-fill"
                  data-series={(index % 4) + 1}
                  style={{ inlineSize: `${pct}%` }}
                />
              </div>
            </li>
          );
        })}
      </ul>
    </WidgetFrame>
  );
}
