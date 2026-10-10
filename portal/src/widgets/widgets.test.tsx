import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { defaultDashboard, type WidgetInstance } from '../config/dashboard';
import { getWidget } from '.';
import { summaryEnvelope } from '../test/fixtures/summary';

afterEach(cleanup);

function renderWidget(instance: WidgetInstance, lang: 'ar' | 'en' = 'en') {
  const Widget = getWidget(instance.type);
  if (!Widget) throw new Error('missing widget ' + instance.type);
  return render(
    <Widget data={summaryEnvelope.data} envelope={summaryEnvelope} instance={instance} lang={lang} />,
  );
}

describe('widgets', () => {
  it('kpi shows label, value and hint', () => {
    renderWidget({ id: 'k', type: 'kpi', options: { metric: 'delivery_rate' } });
    expect(screen.getByText('Delivery rate')).toBeTruthy();
    expect(screen.getByText('60%')).toBeTruthy();
    expect(screen.getByText('Delivered ÷ cohort orders')).toBeTruthy();
  });

  it('kpi-group shows each metric', () => {
    renderWidget({ id: 'g', type: 'kpi-group', options: { metrics: ['orders_placed', 'value_inflation'] } });
    expect(screen.getByText('200')).toBeTruthy();
    expect(screen.getByText('×1.42')).toBeTruthy();
  });

  it('compare-bars scales to the largest value', () => {
    const { container } = renderWidget({
      id: 'b',
      type: 'compare-bars',
      options: { metrics: ['gross_value', 'delivered_value'] },
    });
    const fills = container.querySelectorAll<HTMLElement>('.bar-fill');
    expect(fills[0].style.inlineSize).toBe('100%');
    expect(parseFloat(fills[1].style.inlineSize)).toBeCloseTo((3000 / 4260) * 100, 3);
  });

  it('funnel shows the step-to-step rate', () => {
    renderWidget({ id: 'f', type: 'funnel', options: { steps: ['cohort_orders', 'cohort_delivered'] } });
    expect(screen.getByText('60% of previous step')).toBeTruthy();
  });

  it('creatives-table renders rows, honours limit, and shows an empty message', () => {
    const opts = { columns: ['ad_id', 'orders', 'delivery_rate'], limit: 1 };
    renderWidget({ id: 'c', type: 'creatives-table', options: opts });
    expect(screen.getByText('ad-111')).toBeTruthy();
    expect(screen.queryByText('ad-222')).toBeNull();
    cleanup();

    const Widget = getWidget('creatives-table')!;
    render(
      <Widget
        data={{ ...summaryEnvelope.data, creatives: [] }}
        envelope={summaryEnvelope}
        instance={{ id: 'c', type: 'creatives-table', options: opts }}
        lang="en"
      />,
    );
    expect(screen.getByText(/No creative reached the minimum/)).toBeTruthy();
  });

  it('window-note shows both windows', () => {
    renderWidget({ id: 'w', type: 'window-note', options: {} });
    expect(screen.getByText('Report window')).toBeTruthy();
    expect(screen.getByText('Cohort window')).toBeTruthy();
  });

  it('every widget type in the default dashboard is registered', () => {
    for (const page of Object.values(defaultDashboard.pages)) {
      for (const w of page!.widgets) expect(getWidget(w.type), w.type).toBeTruthy();
    }
  });
});
