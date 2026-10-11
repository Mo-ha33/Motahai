import { describe, expect, it } from 'vitest';
import { formatMetric } from './format';

describe('formatMetric', () => {
  it('formats en-US', () => {
    expect(formatMetric(1234, 'count', 'en')).toBe('1,234');
    expect(formatMetric(0.6, 'percent', 'en')).toBe('60%');
    expect(formatMetric(450.5, 'money', 'en')).toBe('450.50');
    expect(formatMetric(1.42, 'ratio', 'en')).toBe('×1.42');
  });

  it('uses Arabic locale digits in ar', () => {
    expect(formatMetric(1234, 'count', 'ar')).toMatch(/[٠-٩]/);
    expect(formatMetric(1.42, 'ratio', 'ar')).toMatch(/^×/);
  });

  it('formats money with the envelope currency and falls back to a plain number', () => {
    expect(formatMetric(450.5, 'money', 'en', 'EGP')).toMatch(/EGP/);
    expect(formatMetric(450.5, 'money', 'en', 'USD')).toBe('$450.50');
    expect(formatMetric(450.5, 'money', 'ar', 'EGP')).toMatch(/[٠-٩]/);
    expect(formatMetric(450.5, 'money', 'en', undefined)).toBe('450.50');
    expect(formatMetric(450.5, 'money', 'en', 'not-a-code')).toBe('450.50');
    expect(formatMetric(1234, 'count', 'en', 'EGP')).toBe('1,234');
  });

  it('renders null as an em dash', () => {
    expect(formatMetric(null, 'percent', 'en')).toBe('—');
  });
});
