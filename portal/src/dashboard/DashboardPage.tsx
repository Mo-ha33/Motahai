import type { ReactNode } from 'react';
import type { ApiError } from '../api/client';
import { pageSources, widgetSource, type PageConfig, type WidgetInstance } from '../config/dashboard';
import { getFilter } from '../filters/registry';
import { useFilters } from '../filters/FiltersContext';
import { dictionaries, interpolate, type Lang } from '../i18n';
import { getWidget } from '../widgets';
import { StatsProvider, useStats, type SourceState } from './StatsProvider';

export function DashboardPage({ config, lang }: { config: PageConfig; lang: Lang }) {
  return (
    <StatsProvider sources={pageSources(config)}>
      <div className="filter-bar">
        {config.filters.map((id) => {
          const { Card } = getFilter(id);
          return <Card key={id} lang={lang} />;
        })}
      </div>
      <DashboardBody config={config} lang={lang} />
    </StatsProvider>
  );
}

function DashboardBody({ config, lang }: { config: PageConfig; lang: Lang }) {
  const t = dictionaries[lang];
  const { tenantId } = useFilters();
  const { sources, reload } = useStats();
  const ids = pageSources(config);
  const states = ids.map((id) => sources[id]);

  if (states.every((s) => !s || s.status === 'idle')) {
    const needsTenant = tenantId === null;
    return (
      <StateCard
        heading={needsTenant ? t.states.chooseTenant : t.states.fixRange}
        body={needsTenant ? t.states.chooseTenantBody : t.states.fixRangeBody}
      />
    );
  }
  if (states.every((s) => s?.status === 'loading')) {
    return <LoadingCards config={config} label={t.states.loading} />;
  }
  // Every source failed: one page-level card (the first error), as for a summary-only page.
  if (states.every((s) => s?.status === 'error')) {
    const error = states[0]?.error;
    if (error) {
      return (
        <StateCard heading={t.states.errorHeading} body={errorCopy(error, lang)} role="alert">
          <button type="button" className="button" onClick={reload}>
            {t.states.retry}
          </button>
        </StateCard>
      );
    }
  }
  return (
    <div className="widget-grid">
      {config.widgets.map((instance) => (
        <div key={instance.id} className="widget-cell" data-span={instance.span ?? 12}>
          <WidgetSlot
            instance={instance}
            state={sources[widgetSource(instance)]}
            lang={lang}
            onRetry={reload}
          />
        </div>
      ))}
    </div>
  );
}

function WidgetSlot({
  instance,
  state,
  lang,
  onRetry,
}: {
  instance: WidgetInstance;
  state: SourceState | undefined;
  lang: Lang;
  onRetry: () => void;
}) {
  const t = dictionaries[lang];
  const Widget = getWidget(instance.type);
  if (!Widget) {
    return (
      <section className="widget widget-error" role="alert">
        {interpolate(t.widgetErrors.unknownType, { type: instance.type })}
      </section>
    );
  }
  if (!state || state.status === 'loading' || state.status === 'idle') {
    return <div className="widget skeleton" role="status" aria-busy="true" aria-label={t.states.loading} />;
  }
  if (state.status === 'error' && state.error) {
    return (
      <section className="widget widget-source-error" role="alert" data-widget={instance.type}>
        {instance.titleKey ? <h2 className="widget-title">{t.widgetTitles[instance.titleKey]}</h2> : null}
        <p className="widget-note">{t.states.errorHeading}</p>
        <p className="widget-note">{errorCopy(state.error, lang)}</p>
        <button type="button" className="button" onClick={onRetry}>
          {t.states.retry}
        </button>
      </section>
    );
  }
  if (!state.envelope) return null;
  return <Widget data={state.envelope.data} envelope={state.envelope} instance={instance} lang={lang} />;
}

export function errorCopy(error: ApiError, lang: Lang): string {
  const t = dictionaries[lang].states;
  switch (error.status) {
    case 401:
      return t.error401;
    case 404:
      return t.error404;
    case 422:
      return error.detail;
    case 0:
      return t.error0;
    default:
      return interpolate(t.errorOther, { status: error.status });
  }
}

function StateCard({
  heading,
  body,
  role,
  children,
}: {
  heading: string;
  body: string;
  role?: 'alert';
  children?: ReactNode;
}) {
  return (
    <section className="card state-card" role={role} aria-live="polite">
      <h2>{heading}</h2>
      <p>{body}</p>
      {children}
    </section>
  );
}

function LoadingCards({ config, label }: { config: PageConfig; label: string }) {
  return (
    <div className="widget-grid" role="status" aria-label={label} aria-busy="true">
      {config.widgets.map((instance) => (
        <div key={instance.id} className="widget-cell" data-span={instance.span ?? 12}>
          <div className="widget skeleton" />
        </div>
      ))}
    </div>
  );
}
