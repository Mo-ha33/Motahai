import type { MetricId } from '../config/metrics';
import { formatMetric, NO_VALUE } from '../format';
import { dictionaries, interpolate } from '../i18n';
import type { WidgetProps } from './registry';
import { MetricValue, resolveMetric, WidgetFrame } from './shared';

export interface FunnelOptions {
  steps: MetricId[];
}

export function FunnelWidget({ data, envelope, instance, lang }: WidgetProps<FunnelOptions>) {
  const t = dictionaries[lang];
  const steps = instance.options.steps.map((id) => ({ id, metric: resolveMetric(id, data, lang, envelope.currency) }));
  const first = steps[0]?.metric.value ?? 0;
  return (
    <WidgetFrame instance={instance} lang={lang}>
      <ol className="funnel">
        {steps.map(({ id, metric }, index) => {
          const prev = index > 0 ? steps[index - 1].metric.value : null;
          const stepRate =
            index > 0 && prev && metric.value !== null ? metric.value / prev : null;
          const width = first > 0 && metric.value !== null ? (metric.value / first) * 100 : 0;
          return (
            <li className="funnel-step" key={id} data-metric={id}>
              <div className="bar-head">
                <span className="bar-label">{metric.label}</span>
                <MetricValue metric={metric} lang={lang} />
              </div>
              <div className="bar-track">
                <div className="bar-fill" data-series={1} style={{ inlineSize: `${width}%` }} />
              </div>
              {index > 0 ? (
                <div className="funnel-rate" title={stepRate === null ? t.noData : undefined}>
                  {stepRate === null
                    ? NO_VALUE
                    : interpolate(t.funnel.stepRate, {
                        pct: formatMetric(stepRate, 'percent', lang),
                      })}
                </div>
              ) : null}
            </li>
          );
        })}
      </ol>
    </WidgetFrame>
  );
}
