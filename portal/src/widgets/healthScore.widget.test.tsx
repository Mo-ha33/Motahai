import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import type { WidgetInstance } from '../config/dashboard';
import { localeFor } from '../format';
import type { HealthScoreData, StatsEnvelope } from '../api/types';
import { getWidget } from '.';
import {
  criticalHealthData,
  degradedHealthData,
  healthEnvelope,
  healthyHealthData,
  insufficientHealthData,
  noDeductionsHealthData,
} from '../test/fixtures/healthScore';

const MINUS = '−';
const instance = (options: Record<string, unknown> = {}): WidgetInstance => ({
  id: 'h',
  type: 'health-score',
  titleKey: 'healthScore',
  source: 'health-score',
  options,
});

function renderHealth(
  data: HealthScoreData,
  lang: 'ar' | 'en' = 'en',
  options: Record<string, unknown> = {},
) {
  const Widget = getWidget('health-score')!;
  const envelope: StatsEnvelope<HealthScoreData> = healthEnvelope(data);
  return render(<Widget data={data} envelope={envelope} instance={instance(options)} lang={lang} />);
}

function chip(container: HTMLElement): HTMLElement {
  const el = container.querySelector<HTMLElement>('.health-chip');
  if (!el) throw new Error('no status chip');
  return el;
}

function penaltyRows(container: HTMLElement) {
  return Array.from(container.querySelectorAll<HTMLElement>('.health-penalty')).map((row) => ({
    label: row.querySelector('.health-penalty-label')?.textContent ?? '',
    evidence: row.querySelector('.health-penalty-evidence')?.textContent ?? null,
    points: row.querySelector('.health-penalty-points')?.textContent ?? '',
  }));
}

afterEach(cleanup);

describe('health-score widget: status chip', () => {
  it.each([
    ['en', 'healthy', healthyHealthData, 'Healthy', 'positive'],
    ['en', 'degraded', degradedHealthData, 'Degraded', 'warning'],
    ['en', 'critical', criticalHealthData, 'Critical', 'negative'],
    ['ar', 'healthy', healthyHealthData, 'سليمة', 'positive'],
    ['ar', 'degraded', degradedHealthData, 'متراجعة', 'warning'],
    ['ar', 'critical', criticalHealthData, 'حرجة', 'negative'],
  ] as const)('%s %s chip', (lang, _status, data, text, tone) => {
    const { container } = renderHealth(data, lang);
    expect(chip(container).textContent).toBe(text);
    expect(chip(container).dataset.tone).toBe(tone);
    expect(container.querySelector('.health-gauge')?.getAttribute('data-tone')).toBe(tone);
  });

  it('shows the score number and "out of 100" for a scored window', () => {
    const { container } = renderHealth(degradedHealthData);
    expect(container.querySelector('.health-score-value')?.textContent).toBe('72');
    expect(container.querySelector('.health-score-max')?.textContent).toBe('out of 100');
  });
});

describe('health-score widget: insufficient data', () => {
  it('shows no number, no 0 or 100, and the explanation (en)', () => {
    const { container } = renderHealth(insufficientHealthData);
    expect(chip(container).textContent).toBe('Insufficient data');
    expect(chip(container).dataset.tone).toBe('none');
    expect(container.querySelector('.health-score-value')).toBeNull();
    expect(container.querySelector('.health-score-max')).toBeNull();
    expect(screen.getByText('Not enough tracking data in this window to score.')).toBeTruthy();
    expect(screen.queryByText('0')).toBeNull();
    expect(screen.queryByText('100')).toBeNull();
    expect(container.textContent).not.toMatch(/\d/);
    expect(container.querySelector('.health-gauge-fill')).toBeNull();
  });

  it('shows the explanation in Arabic', () => {
    const { container } = renderHealth(insufficientHealthData, 'ar');
    expect(chip(container).textContent).toBe('بيانات غير كافية');
    expect(screen.getByText('لا توجد بيانات تتبّع كافية في هذه الفترة لحساب الدرجة.')).toBeTruthy();
    expect(container.querySelector('.health-score-value')).toBeNull();
    expect(container.textContent).not.toMatch(/\d/);
  });

  it('shows no deductions list even when the response carries penalties', () => {
    const { container } = renderHealth({ ...insufficientHealthData, penalties: degradedHealthData.penalties });
    expect(container.querySelector('.health-penalties')).toBeNull();
  });
});

describe('health-score widget: penalties', () => {
  it('sorts penalties by points, descending, each with a "−N" value', () => {
    const { container } = renderHealth(criticalHealthData);
    const rows = penaltyRows(container);
    expect(rows.map((r) => r.label)).toEqual(['Critical incidents', 'Job failures', 'Scheduler stale']);
    expect(rows.map((r) => r.points)).toEqual([`${MINUS}40`, `${MINUS}15`, `${MINUS}15`]);
  });

  it('sorts the degraded fixture (given unsorted) with the largest deduction first', () => {
    const { container } = renderHealth(degradedHealthData);
    const rows = penaltyRows(container);
    expect(rows.map((r) => r.label)).toEqual(['Flagged events', 'Error incidents']);
    expect(rows.map((r) => r.points)).toEqual([`${MINUS}18`, `${MINUS}10`]);
  });

  it('renders the points in Arabic digits with the minus sign', () => {
    const { container } = renderHealth(degradedHealthData, 'ar');
    const nf = new Intl.NumberFormat(localeFor('ar'), { maximumFractionDigits: 0 });
    const rows = penaltyRows(container);
    expect(rows[0].label).toBe('أحداث مُعلَّمة');
    expect(rows[0].points).toBe(`${MINUS}${nf.format(18)}`);
  });

  it('shows evidence lines built from the counts (en)', () => {
    const { container } = renderHealth(degradedHealthData);
    expect(screen.getByText('12 of 340 events flagged')).toBeTruthy();
    expect(screen.getByText('1 open incident')).toBeTruthy();
    expect(penaltyRows(container).map((r) => r.evidence)).toEqual([
      '12 of 340 events flagged',
      '1 open incident',
    ]);
  });

  it('shows the scheduler-stale age in hours and the job-failure count', () => {
    renderHealth(criticalHealthData);
    expect(screen.getByText('Last successful run 2 h ago')).toBeTruthy();
    expect(screen.getByText('5 failed job runs')).toBeTruthy();
    expect(screen.getByText('2 open incidents')).toBeTruthy();
  });

  it('uses the "no successful run" evidence when the scheduler never ran', () => {
    renderHealth({
      score: 85,
      status: 'healthy',
      penalties: [{ name: 'scheduler_stale', points: 15, evidence: { last_success_found: false, age_seconds: null } }],
    });
    expect(screen.getByText('No successful run found')).toBeTruthy();
  });

  it('renders an unknown penalty name as its raw name, with its points and no evidence line', () => {
    const { container } = renderHealth({
      score: 95,
      status: 'healthy',
      penalties: [{ name: 'future_signal', points: 5, evidence: { anything: 3 } }],
    });
    const rows = penaltyRows(container);
    expect(rows).toEqual([{ label: 'future_signal', evidence: null, points: `${MINUS}5` }]);
  });

  it('shows "No deductions." when the score has no penalties', () => {
    const { container } = renderHealth(noDeductionsHealthData);
    expect(screen.getByText('No deductions.')).toBeTruthy();
    expect(container.querySelector('.health-penalty-list')).toBeNull();
    expect(container.querySelector('.health-score-value')?.textContent).toBe('100');
  });

  it('shows "لا توجد خصومات." in Arabic when there are no penalties', () => {
    renderHealth(noDeductionsHealthData, 'ar');
    expect(screen.getByText('لا توجد خصومات.')).toBeTruthy();
  });

  it('hides the deductions list entirely when showPenalties is false', () => {
    const { container } = renderHealth(degradedHealthData, 'en', { showPenalties: false });
    expect(container.querySelector('.health-penalties')).toBeNull();
    expect(screen.queryByText('Deductions')).toBeNull();
    expect(screen.queryByText('Flagged events')).toBeNull();
    expect(screen.getByText('72')).toBeTruthy();
  });

  it('shows the list by default and when showPenalties is true', () => {
    const { container } = renderHealth(degradedHealthData, 'en', { showPenalties: true });
    expect(within(container).getByText('Deductions')).toBeTruthy();
    expect(penaltyRows(container)).toHaveLength(2);
  });
});
