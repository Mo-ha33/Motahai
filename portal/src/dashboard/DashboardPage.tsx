import type { ReactNode } from 'react';
import type { ApiError } from '../api/client';
import type { PageConfig, WidgetInstance } from '../config/dashboard';
import { getFilter } from '../filters/registry';
import { useFilters } from '../filters/FiltersContext';
import { dictionaries, interpolate, type Lang } from '../i18n';
import { getWidget } from '../widgets';
import { StatsProvider, useStats } from './StatsProvider';

export function DashboardPage({ config, lang }: { config: PageConfig; lang: Lang }) {
  return (
    <StatsProvider>
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
  const { status, envelope, error, reload } = useStats();

  if (status === 'idle') {
    const needsTenant = tenantId === null;
    return (
      <StateCard
        heading={needsTenant ? t.states.chooseTenant : t.states.fixRange}
        body={needsTenant ? t.states.chooseTenantBody : t.states.fixRangeBody}
      />
    );
  }
  if (status === 'loading') return <LoadingCards config={config} label={t.states.loading} />;
  if (status === 'error' && error) {
    return (
      <StateCard heading={t.states.errorHeading} body={errorCopy(error, lang)} role="alert">
        <button type="button" className="button" onClick={reload}>
          {t.states.retry}
        </button>
      </StateCard>
    );
  }
  if (!envelope) return null;

  return (
    <div className="widget-grid">
      {config.widgets.map((instance) => (
        <div
          key={instance.id}
          className="widget-cell"
          data-span={instance.span ?? 12}
        >
          <WidgetSlot instance={instance} envelope={envelope} lang={lang} />
        </div>
      ))}
    </div>
  );
}

function WidgetSlot({
  instance,
  envelope,
  lang,
}: {
  instance: WidgetInstance;
  envelope: NonNullable<ReturnType<typeof useStats>['envelope']>;
  lang: Lang;
}) {
  const Widget = getWidget(instance.type);
  if (!Widget) {
    return (
      <section className="widget widget-error" role="alert">
        {interpolate(dictionaries[lang].widgetErrors.unknownType, { type: instance.type })}
      </section>
    );
  }
  return <Widget data={envelope.data} envelope={envelope} instance={instance} lang={lang} />;
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
