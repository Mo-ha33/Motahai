import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  clearSession,
  isValidKeyFormat,
  loadSession,
  saveSession,
  SESSION_STORAGE_KEY,
} from './session';

export const KEY = 'mtk_0a1b2c3d_' + 'A'.repeat(43);
const PROFILE = { tenant_id: 7, name: 'Acme', currency: 'EGP', country: 'EG', mode: 'live', platform: 'shopify' };

describe('session store', () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    window.localStorage.clear();
  });
  afterEach(() => vi.restoreAllMocks());

  it('validates the key format client-side', () => {
    expect(isValidKeyFormat(KEY)).toBe(true);
    expect(isValidKeyFormat('mtk_0a1b2c3d_abc-DEF_1234567890')).toBe(true);
    for (const bad of ['', 'mtk_', 'mtk_0a1b2c3_' + 'A'.repeat(43), 'mtk_0A1B2C3D_' + 'A'.repeat(43),
      'mtk_0a1b2c3d' + 'A'.repeat(43), 'xyz_0a1b2c3d_' + 'A'.repeat(43), 'mtk_0a1b2c3d_short', KEY + ' x']) {
      expect(isValidKeyFormat(bad)).toBe(false);
    }
  });

  it('stores a tenant session in sessionStorage only, never localStorage', () => {
    saveSession({ mode: 'tenant', key: KEY, profile: PROFILE });
    expect(window.sessionStorage.getItem(SESSION_STORAGE_KEY)).toContain(KEY);
    expect(window.localStorage.length).toBe(0);
    expect(loadSession()).toEqual({ mode: 'tenant', key: KEY, profile: PROFILE });
    clearSession();
    expect(loadSession()).toBeNull();
    expect(window.sessionStorage.length).toBe(0);
  });

  it('round-trips operator mode', () => {
    saveSession({ mode: 'operator' });
    expect(loadSession()).toEqual({ mode: 'operator' });
  });

  it('discards corrupt or malformed entries', () => {
    for (const raw of ['not json', '{"mode":"tenant","key":"bad","profile":{}}',
      JSON.stringify({ mode: 'tenant', key: KEY, profile: { tenant_id: 'x' } }), '{"mode":"admin"}']) {
      window.sessionStorage.setItem(SESSION_STORAGE_KEY, raw);
      expect(loadSession()).toBeNull();
    }
    expect(window.sessionStorage.getItem(SESSION_STORAGE_KEY)).toBeNull();
  });

  it('survives blocked storage', () => {
    vi.spyOn(window, 'sessionStorage', 'get').mockImplementation(() => {
      throw new Error('blocked');
    });
    expect(loadSession()).toBeNull();
    expect(() => saveSession({ mode: 'operator' })).not.toThrow();
    expect(() => clearSession()).not.toThrow();
  });
});
