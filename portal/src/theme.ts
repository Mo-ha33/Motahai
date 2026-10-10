export type ThemeMode = 'auto' | 'light' | 'dark';

export const THEME_MODES: readonly ThemeMode[] = ['auto', 'light', 'dark'];
export const DEFAULT_THEME_MODE: ThemeMode = 'auto';

const STORAGE_KEY = 'motahai.theme';

export function nextThemeMode(mode: ThemeMode): ThemeMode {
  return THEME_MODES[(THEME_MODES.indexOf(mode) + 1) % THEME_MODES.length];
}

export function readStoredThemeMode(): ThemeMode {
  try {
    const value = window.localStorage.getItem(STORAGE_KEY);
    if (value === 'auto' || value === 'light' || value === 'dark') return value;
  } catch {
    // Storage may be blocked; fall back to auto.
  }
  return DEFAULT_THEME_MODE;
}

export function storeThemeMode(mode: ThemeMode): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, mode);
  } catch {
    // Non-fatal: the choice simply won't persist.
  }
}

/** `auto` follows the OS (no data-theme attribute); light/dark force a theme. */
export function applyThemeMode(mode: ThemeMode): void {
  const root = document.documentElement;
  if (mode === 'auto') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', mode);
}

/** Overrides design tokens on :root, e.g. `{ '--color-accent': '#c2410c' }`. Keys must start with `--`. */
export function applyTokenOverrides(overrides: Partial<Record<string, string>>): void {
  const root = document.documentElement;
  for (const [name, value] of Object.entries(overrides)) {
    if (!name.startsWith('--')) throw new Error(`Token override must start with "--": ${name}`);
    if (value === undefined) root.style.removeProperty(name);
    else root.style.setProperty(name, value);
  }
}
