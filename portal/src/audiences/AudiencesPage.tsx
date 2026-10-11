import { useCallback, useEffect, useState, type ReactNode } from 'react';
import { ApiError } from '../api/client';
import type { AudienceListName, AudienceListStatus, AudiencesStatus } from '../api/types';
import { useSession } from '../auth/SessionContext';
import { useAudiencesClient } from '../dashboard/ApiContext';
import { useFilters } from '../filters/FiltersContext';
import { getFilter } from '../filters/registry';
import { formatMetric, localeFor, NO_VALUE } from '../format';
import { dictionaries, interpolate, type Dictionary, type Lang } from '../i18n';

type T = Dictionary['audiences'];

type LoadState =
  | { kind: 'idle' }
  | { kind: 'loading' }
  | { kind: 'ready'; status: AudiencesStatus }
  | { kind: 'forbidden' }
  | { kind: 'error'; status: number };

function formatDateTime(iso: string | null, lang: Lang): string {
  if (iso === null) return NO_VALUE;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return NO_VALUE;
  return new Intl.DateTimeFormat(localeFor(lang), { dateStyle: 'medium', timeStyle: 'short', timeZone: 'UTC' }).format(
    date,
  );
}

function errorBody(t: T, status: number): string {
  switch (status) {
    case 0:
      return t.error0;
    case 401:
      return t.error401;
    case 404:
      return t.error404;
    default:
      return interpolate(t.errorOther, { status });
  }
}

/** Saves a Blob through a temporary object URL, revoked right after the click. */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  try {
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = filename;
    anchor.rel = 'noopener';
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  } finally {
    // Some browsers cancel the download if the URL is revoked synchronously after click().
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
}

export function AudiencesPage({ lang }: { lang: Lang }) {
  const t = dictionaries[lang].audiences;
  const { session } = useSession();
  const client = useAudiencesClient();
  const { tenantId } = useFilters();
  const operator = session?.mode === 'operator';
  const [state, setState] = useState<LoadState>({ kind: 'idle' });
  const [nonce, setNonce] = useState(0);
  const [exporting, setExporting] = useState(false);
  const [exportFailed, setExportFailed] = useState(false);

  useEffect(() => {
    if (tenantId === null) {
      setState({ kind: 'idle' });
      return undefined;
    }
    const controller = new AbortController();
    setState({ kind: 'loading' });
    client.status(tenantId, controller.signal).then(
      (status) => setState({ kind: 'ready', status }),
      (err: unknown) => {
        if (controller.signal.aborted) return;
        const code = err instanceof ApiError ? err.status : 0;
        // Tenant mode: a 401 here means the key lacks `audiences:read` (it is valid for stats), so do NOT sign out.
        setState(code === 401 && !operator ? { kind: 'forbidden' } : { kind: 'error', status: code });
      },
    );
    return () => controller.abort();
  }, [client, tenantId, nonce, operator]);

  const exportNow = useCallback(async () => {
    if (tenantId === null) return;
    setExporting(true);
    setExportFailed(false);
    try {
      const status = await client.exportNow(tenantId);
      setState({ kind: 'ready', status });
    } catch {
      setExportFailed(true);
    } finally {
      setExporting(false);
    }
  }, [client, tenantId]);

  const { Card: TenantCard } = getFilter('tenant');

  return (
    <>
      <div className="filter-bar">
        <TenantCard lang={lang} />
      </div>
      {state.kind === 'idle' ? (
        <StateCard heading={t.chooseTenant} body={t.chooseTenantBody} />
      ) : state.kind === 'loading' ? (
        <StateCard heading={t.loading} body="" busy />
      ) : state.kind === 'forbidden' ? (
        <StateCard heading={t.forbiddenHeading} body={t.forbidden} role="alert" />
      ) : state.kind === 'error' ? (
        <StateCard heading={t.errorHeading} body={errorBody(t, state.status)} role="alert">
          <button type="button" className="button" onClick={() => setNonce((n) => n + 1)}>
            {t.retry}
          </button>
        </StateCard>
      ) : tenantId !== null ? (
        <div className="audience-grid">
          {state.status.lists.map((list) => (
            <ListCard
              key={list.name}
              tenantId={tenantId}
              list={list}
              minRows={state.status.min_list_rows}
              lang={lang}
            />
          ))}
          <ScheduleCard status={state.status} lang={lang} />
          {operator ? (
            <div className="audience-actions">
              <button type="button" className="button button-primary" onClick={exportNow} disabled={exporting}>
                {exporting ? t.exporting : t.exportNow}
              </button>
              {exportFailed ? (
                <p className="audience-error" role="alert">
                  {t.exportFailed}
                </p>
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}
    </>
  );
}

function StateCard({
  heading,
  body,
  role,
  busy,
  children,
}: {
  heading: string;
  body: string;
  role?: 'alert';
  busy?: boolean;
  children?: ReactNode;
}) {
  return (
    <section className="card empty-state" role={role} aria-live={role ? undefined : 'polite'} aria-busy={busy}>
      <h2>{heading}</h2>
      {body ? <p>{body}</p> : null}
      {children}
    </section>
  );
}

function ListCard({
  tenantId,
  list,
  minRows,
  lang,
}: {
  tenantId: number;
  list: AudienceListStatus;
  minRows: number;
  lang: Lang;
}) {
  const t = dictionaries[lang].audiences;
  const client = useAudiencesClient();
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const copy = t.lists[list.name as AudienceListName];
  const exported = list.rows !== null;
  const small = list.rows !== null && list.rows < minRows;

  const download = async () => {
    setBusy(true);
    setFailed(false);
    try {
      const blob = await client.download(tenantId, list.name);
      saveBlob(blob, `${tenantId}_${list.name}.csv`);
    } catch {
      setFailed(true);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="card audience-card" aria-labelledby={`audience-${list.name}`}>
      <h2 id={`audience-${list.name}`}>{copy.title}</h2>
      <p>{copy.purpose}</p>
      <dl className="audience-facts">
        <div>
          <dt>{t.rows}</dt>
          <dd>{list.rows === null ? t.neverExported : formatMetric(list.rows, 'count', lang)}</dd>
        </div>
        <div>
          <dt>{t.updated}</dt>
          <dd>{formatDateTime(list.updated_at, lang)}</dd>
        </div>
      </dl>
      {small ? (
        <p className="audience-warning" role="status">
          {interpolate(t.smallWarning, { min: formatMetric(minRows, 'count', lang) })}
        </p>
      ) : null}
      {!exported ? <p>{t.notExported}</p> : null}
      <button type="button" className="button" onClick={download} disabled={busy || !exported}>
        {busy ? t.downloading : t.download}
      </button>
      {failed ? (
        <p className="audience-error" role="alert">
          {t.downloadFailed}
        </p>
      ) : null}
      <p className="audience-note">{t.hashesNote}</p>
    </section>
  );
}

function ScheduleCard({ status, lang }: { status: AudiencesStatus; lang: Lang }) {
  const t = dictionaries[lang].audiences;
  const { schedule } = status;
  const ran = schedule.last_run_at !== null;
  const outcome =
    schedule.last_run_ok === true
      ? t.schedule.ok
      : schedule.last_error_type
        ? interpolate(t.schedule.failedWithType, { type: schedule.last_error_type })
        : t.schedule.failed;
  return (
    <section className="card audience-card" aria-labelledby="audience-schedule">
      <h2 id="audience-schedule">{t.schedule.title}</h2>
      <dl className="audience-facts">
        <div>
          <dt>{t.schedule.lastRun}</dt>
          <dd>
            {ran ? formatDateTime(schedule.last_run_at, lang) : t.schedule.never}
            {ran ? (
              <span className={schedule.last_run_ok === true ? 'audience-ok' : 'audience-error'}> · {outcome}</span>
            ) : null}
          </dd>
        </div>
        <div>
          <dt>{t.schedule.nextDue}</dt>
          <dd>{formatDateTime(schedule.next_due_at, lang)}</dd>
        </div>
      </dl>
    </section>
  );
}
