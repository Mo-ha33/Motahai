import type { StatsWindow } from '../api/types';

export const PRESET_DAYS = [7, 14, 30, 90] as const;
export const MAX_RANGE_DAYS = 92;
const DAY_MS = 86_400_000;

export type RangeError = 'invalid' | 'order' | 'span';

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

function parseDay(value: string): number | null {
  if (!ISO_DATE.test(value)) return null;
  const ms = Date.parse(`${value}T00:00:00Z`);
  return Number.isNaN(ms) ? null : ms;
}

/** Mirrors the API: end must be after start and the span at most 92 days. */
export function validateRange(start: string, end: string): RangeError | null {
  const s = parseDay(start);
  const e = parseDay(end);
  if (s === null || e === null) return 'invalid';
  if (e <= s) return 'order';
  if (e - s > MAX_RANGE_DAYS * DAY_MS) return 'span';
  return null;
}

export function todayUtc(now: Date): string {
  return now.toISOString().slice(0, 10);
}

/** The last `days` full UTC days, ending at today's UTC midnight (the API default shape). */
export function presetRange(days: number, now: Date): StatsWindow {
  const end = todayUtc(now);
  const start = new Date(Date.parse(`${end}T00:00:00Z`) - days * DAY_MS).toISOString().slice(0, 10);
  return { start, end };
}
