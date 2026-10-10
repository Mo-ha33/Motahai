import type { Lang } from './i18n';

export type MetricFormat = 'count' | 'percent' | 'money' | 'ratio';

export const NO_VALUE = '—';

const LOCALES: Record<Lang, string> = { ar: 'ar-EG', en: 'en-US' };

export function localeFor(lang: Lang): string {
  return LOCALES[lang];
}

/** Formats a metric value. `null` (undefined ratio) renders as an em dash. The API has no currency, so money is a plain number. */
export function formatMetric(value: number | null, format: MetricFormat, lang: Lang): string {
  if (value === null || !Number.isFinite(value)) return NO_VALUE;
  const locale = localeFor(lang);
  switch (format) {
    case 'count':
      return new Intl.NumberFormat(locale, { maximumFractionDigits: 0 }).format(value);
    case 'percent':
      return new Intl.NumberFormat(locale, {
        style: 'percent',
        minimumFractionDigits: 0,
        maximumFractionDigits: 1,
      }).format(value);
    case 'money':
      return new Intl.NumberFormat(locale, {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      }).format(value);
    case 'ratio':
      return `×${new Intl.NumberFormat(locale, {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      }).format(value)}`;
  }
}

/** Formats an ISO date/datetime as a UTC calendar date. */
export function formatDate(iso: string, lang: Lang): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return new Intl.DateTimeFormat(localeFor(lang), {
    dateStyle: 'medium',
    timeZone: 'UTC',
  }).format(date);
}
