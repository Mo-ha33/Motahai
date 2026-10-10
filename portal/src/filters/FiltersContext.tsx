import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import type { StatsWindow } from '../api/types';
import { PRESET_DAYS, presetRange, validateRange, type RangeError } from './range';

export type RangePreset = (typeof PRESET_DAYS)[number] | 'custom';

export interface RangeState {
  preset: RangePreset;
  start: string;
  end: string;
}

const STORAGE_KEY = 'motahai.filters';
const DEFAULT_PRESET = 7;

export interface FiltersValue {
  /** Raw text of the tenant input. */
  tenantInput: string;
  /** Parsed tenant id, or null when empty/invalid. */
  tenantId: number | null;
  range: RangeState;
  rangeError: RangeError | null;
  /** The valid window to query, or null when the range is invalid. */
  window: StatsWindow | null;
  setTenantInput(value: string): void;
  setPreset(preset: RangePreset): void;
  setCustomRange(start: string, end: string): void;
}

const FiltersContext = createContext<FiltersValue | null>(null);

export function parseTenantId(input: string): number | null {
  const trimmed = input.trim();
  if (!/^\d+$/.test(trimmed)) return null;
  const value = Number(trimmed);
  return Number.isSafeInteger(value) && value > 0 ? value : null;
}

function isPreset(value: unknown): value is RangePreset {
  return value === 'custom' || (PRESET_DAYS as readonly unknown[]).includes(value);
}

function loadInitial(now: Date): { tenantInput: string; range: RangeState } {
  const fallbackWindow = presetRange(DEFAULT_PRESET, now);
  const fallback = {
    tenantInput: '',
    range: { preset: DEFAULT_PRESET as RangePreset, ...fallbackWindow },
  };
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return fallback;
    const saved = JSON.parse(raw) as {
      tenantId?: unknown;
      range?: { preset?: unknown; start?: unknown; end?: unknown };
    };
    const tenantInput = typeof saved.tenantId === 'string' ? saved.tenantId : '';
    const preset = saved.range?.preset;
    if (preset === 'custom') {
      const { start, end } = saved.range ?? {};
      if (typeof start === 'string' && typeof end === 'string') {
        return { tenantInput, range: { preset: 'custom', start, end } };
      }
    } else if (isPreset(preset)) {
      // Presets are relative to today, so recompute rather than trust stored dates.
      return { tenantInput, range: { preset, ...presetRange(preset as number, now) } };
    }
    return { ...fallback, tenantInput };
  } catch {
    return fallback;
  }
}

export function FiltersProvider({
  children,
  now = () => new Date(),
}: {
  children: ReactNode;
  now?: () => Date;
}) {
  const [state, setState] = useState(() => loadInitial(now()));

  useEffect(() => {
    try {
      window.localStorage.setItem(
        STORAGE_KEY,
        JSON.stringify({ tenantId: state.tenantInput, range: state.range }),
      );
    } catch {
      // Non-fatal: filters simply won't persist.
    }
  }, [state]);

  const setTenantInput = useCallback(
    (tenantInput: string) => setState((s) => ({ ...s, tenantInput })),
    [],
  );
  const setPreset = useCallback(
    (preset: RangePreset) =>
      setState((s) => ({
        ...s,
        range:
          preset === 'custom'
            ? { preset, start: s.range.start, end: s.range.end }
            : { preset, ...presetRange(preset, now()) },
      })),
    [],
  );
  const setCustomRange = useCallback(
    (start: string, end: string) => setState((s) => ({ ...s, range: { preset: 'custom', start, end } })),
    [],
  );

  const value = useMemo<FiltersValue>(() => {
    const rangeError = validateRange(state.range.start, state.range.end);
    return {
      tenantInput: state.tenantInput,
      tenantId: parseTenantId(state.tenantInput),
      range: state.range,
      rangeError,
      window: rangeError ? null : { start: state.range.start, end: state.range.end },
      setTenantInput,
      setPreset,
      setCustomRange,
    };
  }, [state, setTenantInput, setPreset, setCustomRange]);

  return <FiltersContext.Provider value={value}>{children}</FiltersContext.Provider>;
}

export function useFilters(): FiltersValue {
  const value = useContext(FiltersContext);
  if (!value) throw new Error('useFilters must be used inside <FiltersProvider>');
  return value;
}
