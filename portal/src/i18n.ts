export type Lang = 'ar' | 'en';

export const DEFAULT_LANG: Lang = 'ar';

export const dictionaries = {
  ar: {
    productName: 'Motahai',
    language: 'English',
    nav: {
      signal: 'صحة الإشارة',
      roas: 'العائد الإعلاني المُسلَّم',
      audiences: 'الجماهير',
      onboarding: 'الإعداد',
    },
    pages: {
      signal: { title: 'صحة الإشارة' },
      roas: { title: 'العائد الإعلاني المُسلَّم (ROAS)' },
      audiences: { title: 'الجماهير' },
      onboarding: { title: 'الإعداد' },
    },
    emptyState: {
      heading: 'لا توجد بيانات بعد',
      body: 'ستظهر البيانات هنا فور ربط الواجهة البرمجية (API).',
    },
    notFound: {
      heading: 'الصفحة غير موجودة',
      body: 'اختر قسمًا من القائمة الجانبية.',
    },
  },
  en: {
    productName: 'Motahai',
    language: 'العربية',
    nav: {
      signal: 'Signal health',
      roas: 'Delivered ROAS',
      audiences: 'Audiences',
      onboarding: 'Onboarding',
    },
    pages: {
      signal: { title: 'Signal health' },
      roas: { title: 'Delivered ROAS' },
      audiences: { title: 'Audiences' },
      onboarding: { title: 'Onboarding' },
    },
    emptyState: {
      heading: 'No data yet',
      body: 'Data will appear here once the API is connected.',
    },
    notFound: {
      heading: 'Page not found',
      body: 'Choose a section from the sidebar.',
    },
  },
} as const;

export type Dictionary = (typeof dictionaries)[Lang];

export function directionFor(lang: Lang): 'rtl' | 'ltr' {
  return lang === 'ar' ? 'rtl' : 'ltr';
}

const STORAGE_KEY = 'motahai.lang';

export function readStoredLang(): Lang {
  try {
    const value = window.localStorage.getItem(STORAGE_KEY);
    if (value === 'ar' || value === 'en') return value;
  } catch {
    // Storage may be blocked (private mode, disabled site data); fall back to default.
  }
  return DEFAULT_LANG;
}

export function storeLang(lang: Lang): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, lang);
  } catch {
    // Non-fatal: the choice simply won't persist.
  }
}

export function applyLangToDocument(lang: Lang): void {
  const root = document.documentElement;
  root.lang = lang;
  root.dir = directionFor(lang);
}
