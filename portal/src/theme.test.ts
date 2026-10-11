import { afterEach, describe, expect, it } from 'vitest';
import { applyThemeMode, applyTokenOverrides, nextThemeMode } from './theme';

afterEach(() => {
  document.documentElement.removeAttribute('data-theme');
  document.documentElement.removeAttribute('style');
});

describe('theme', () => {
  it('sets and removes data-theme', () => {
    applyThemeMode('dark');
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark');
    applyThemeMode('light');
    expect(document.documentElement.getAttribute('data-theme')).toBe('light');
    applyThemeMode('auto');
    expect(document.documentElement.hasAttribute('data-theme')).toBe(false);
  });

  it('cycles modes', () => {
    expect(nextThemeMode('auto')).toBe('light');
    expect(nextThemeMode('light')).toBe('dark');
    expect(nextThemeMode('dark')).toBe('auto');
  });

  it('applies token overrides and rejects bad keys', () => {
    applyTokenOverrides({ '--color-accent': '#c2410c' });
    expect(document.documentElement.style.getPropertyValue('--color-accent')).toBe('#c2410c');
    expect(() => applyTokenOverrides({ 'color-accent': 'red' })).toThrow();
  });
});
