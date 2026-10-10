import { useEffect, useState } from 'react';
import {
  applyLangToDocument,
  dictionaries,
  readStoredLang,
  storeLang,
  type Lang,
} from './i18n';
import { hrefFor, ROUTES, useHashRoute, type Route } from './router';

export default function App() {
  const [lang, setLang] = useState<Lang>(() => readStoredLang());
  const route = useHashRoute('signal');
  const t = dictionaries[lang];

  useEffect(() => {
    applyLangToDocument(lang);
    storeLang(lang);
  }, [lang]);

  const toggleLang = () => setLang((current) => (current === 'ar' ? 'en' : 'ar'));

  const pageTitle = route ? t.pages[route].title : t.productName;

  return (
    <div className="app">
      <header className="app-header">
        <span className="brand">{t.productName}</span>
        <button
          type="button"
          className="lang-toggle"
          onClick={toggleLang}
          lang={lang === 'ar' ? 'en' : 'ar'}
        >
          {t.language}
        </button>
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
          <EmptyState lang={lang} route={route} />
        </main>
      </div>
    </div>
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
