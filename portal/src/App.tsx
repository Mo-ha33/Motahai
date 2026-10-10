import { useEffect, useState } from 'react';
import {
  applyLangToDocument,
  dictionaries,
  readStoredLang,
  storeLang,
  type Lang,
} from './i18n';
import { defaultDashboard, type DashboardConfig } from './config/dashboard';
import { DashboardPage } from './dashboard/DashboardPage';
import { FiltersProvider } from './filters/FiltersContext';
import {
  applyThemeMode,
  nextThemeMode,
  readStoredThemeMode,
  storeThemeMode,
  type ThemeMode,
} from './theme';
import { hrefFor, ROUTES, useHashRoute, type Route } from './router';

export default function App({ config = defaultDashboard }: { config?: DashboardConfig }) {
  const [lang, setLang] = useState<Lang>(() => readStoredLang());
  const [themeMode, setThemeMode] = useState<ThemeMode>(() => readStoredThemeMode());
  const route = useHashRoute('signal');
  const t = dictionaries[lang];

  useEffect(() => {
    applyLangToDocument(lang);
    storeLang(lang);
  }, [lang]);

  useEffect(() => {
    applyThemeMode(themeMode);
    storeThemeMode(themeMode);
  }, [themeMode]);

  const toggleLang = () => setLang((current) => (current === 'ar' ? 'en' : 'ar'));

  const page = route ? config.pages[route] : undefined;
  const pageTitle = route ? t.pages[route].title : t.productName;

  return (
    <FiltersProvider>
    <div className="app">
      <header className="app-header">
        <span className="brand">{t.productName}</span>
        <div className="header-actions">
        <button
          type="button"
          className="lang-toggle"
          onClick={() => setThemeMode(nextThemeMode)}
          aria-label={`${t.theme.label}: ${t.theme[themeMode]}`}
        >
          {t.theme.label}: {t.theme[themeMode]}
        </button>
        <button
          type="button"
          className="lang-toggle"
          onClick={toggleLang}
          lang={lang === 'ar' ? 'en' : 'ar'}
        >
          {t.language}
        </button>
        </div>
      </header>

      <div className="app-body">
        <nav className="sidebar" aria-label={t.productName}>
          <ul>
            {ROUTES.map((item) => (
              <li key={item}>
                <a
                  href={hrefFor(item)}
                  className="nav-link"
                  aria-current={route === item ? 'page' : undefined}
                >
                  {t.nav[item]}
                </a>
              </li>
            ))}
          </ul>
        </nav>

        <main className="content">
          <h1 className="page-title">{pageTitle}</h1>
          {page ? (
            <DashboardPage key={route} config={page} lang={lang} />
          ) : (
            <EmptyState lang={lang} route={route} />
          )}
        </main>
      </div>
    </div>
    </FiltersProvider>
  );
}

function EmptyState({ lang, route }: { lang: Lang; route: Route | null }) {
  const t = dictionaries[lang];
  const copy = route ? t.emptyState : t.notFound;
  return (
    <section className="card empty-state" aria-live="polite">
      <h2>{copy.heading}</h2>
      <p>{copy.body}</p>
    </section>
  );
}
