import { useEffect, useState } from 'react';
import {
  applyLangToDocument,
  dictionaries,
  readStoredLang,
  storeLang,
  type Lang,
} from './i18n';
import { defaultDashboard, type DashboardConfig } from './config/dashboard';
import { ApiProvider } from './dashboard/ApiContext';
import { SessionProvider, useSession } from './auth/SessionContext';
import { SignIn } from './auth/SignIn';
import { OnboardingPage } from './onboarding/OnboardingPage';
import { AudiencesPage } from './audiences/AudiencesPage';
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

export default function App({
  config = defaultDashboard,
  fetchImpl,
}: {
  config?: DashboardConfig;
  /** Tests inject a fake fetch; the app uses the global one. */
  fetchImpl?: typeof fetch;
}) {
  return (
    <SessionProvider fetchImpl={fetchImpl}>
      <ApiProvider fetchImpl={fetchImpl}>
        <Shell config={config} />
      </ApiProvider>
    </SessionProvider>
  );
}

function Shell({ config }: { config: DashboardConfig }) {
  const { session, signOut } = useSession();
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

  const identity =
    session?.mode === 'tenant' ? session.profile.name : session ? t.auth.operator : null;

  return (
    <FiltersProvider>
    <div className="app">
      <header className="app-header">
        <span className="brand">{t.productName}</span>
        <div className="header-actions">
        {identity !== null ? (
          <>
            <span className="session-identity">{identity}</span>
            <button type="button" className="lang-toggle" onClick={() => signOut()}>
              {t.auth.signOut}
            </button>
          </>
        ) : null}
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

      {session === null ? (
        <SignIn lang={lang} />
      ) : (
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
          {route === 'onboarding' ? (
            <OnboardingPage lang={lang} />
          ) : route === 'audiences' ? (
            <AudiencesPage lang={lang} />
          ) : page ? (
            <DashboardPage key={route} config={page} lang={lang} />
          ) : (
            <EmptyState lang={lang} route={route} />
          )}
        </main>
      </div>
      )}
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
