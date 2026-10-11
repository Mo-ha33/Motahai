/** Only the numeric tenant id is kept (sessionStorage), so a reload resumes at the checklist. Never any secret. */
export const TENANT_STORAGE_KEY = 'motahai.onboarding.tenant';

export function readStoredTenant(): number | null {
  try {
    const raw = window.sessionStorage.getItem(TENANT_STORAGE_KEY);
    if (raw !== null && /^\d+$/.test(raw)) {
      const id = Number(raw);
      if (Number.isSafeInteger(id) && id > 0) return id;
    }
  } catch {
    // Storage blocked: the wizard simply doesn't resume.
  }
  return null;
}

export function storeTenant(id: number | null): void {
  try {
    if (id === null) window.sessionStorage.removeItem(TENANT_STORAGE_KEY);
    else window.sessionStorage.setItem(TENANT_STORAGE_KEY, String(id));
  } catch {
    // Non-fatal.
  }
}
