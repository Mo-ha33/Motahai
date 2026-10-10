import { useCallback, useEffect, useState, type FormEvent, type ReactNode } from 'react';
import { ApiError } from '../api/client';
import type {
  ChecklistResponse,
  CourierKind,
  CourierSecretsResponse,
  IssueKeyResponse,
} from '../api/types';
import { useSession } from '../auth/SessionContext';
import { useOnboardingClient } from '../dashboard/ApiContext';
import { dictionaries, interpolate, type Dictionary, type Lang } from '../i18n';
import { CopyField } from './CopyField';
import {
  CREATE_DEFAULTS,
  CREATE_FIELDS,
  META_DEFAULTS,
  META_FIELDS,
  formValid,
  type FieldSpec,
} from './fields';
import { readStoredTenant, storeTenant } from './storage';

type T = Dictionary['onboarding'];
type StepId = 'create' | 'meta' | 'courier' | 'apikey' | 'checklist';
const STEPS: readonly StepId[] = ['create', 'meta', 'courier', 'apikey', 'checklist'];

interface StepError {
  status: number;
  detail: string;
}

function toStepError(err: unknown): StepError {
  if (err instanceof ApiError) return { status: err.status, detail: err.detail };
  return { status: 0, detail: '' };
}

function errorLead(t: T['errors'], status: number): string {
  switch (status) {
    case 0:
      return t.e0;
    case 401:
      return t.e401;
    case 403:
      return t.e403;
    case 404:
      return t.e404;
    case 409:
      return t.e409;
    case 422:
      return t.e422;
    case 503:
      return t.e503;
    default:
      return interpolate(t.other, { status });
  }
}

export function OnboardingPage({ lang }: { lang: Lang }) {
  const t = dictionaries[lang].onboarding;
  const { session } = useSession();
  if (session?.mode !== 'operator') {
    return (
      <section className="card empty-state" aria-live="polite">
        <h2>{t.operatorOnly.heading}</h2>
        <p>{t.operatorOnly.body}</p>
      </section>
    );
  }
  return <Wizard t={t} />;
}

function Wizard({ t }: { t: T }) {
  const client = useOnboardingClient();
  const [tenantId, setTenantId] = useState<number | null>(() => readStoredTenant());
  const [step, setStep] = useState<StepId>(() => (readStoredTenant() !== null ? 'checklist' : 'create'));
  const [checklist, setChecklist] = useState<ChecklistResponse | null>(null);
  const [checklistError, setChecklistError] = useState<StepError | null>(null);
  const [error, setError] = useState<StepError | null>(null);
  const [busy, setBusy] = useState(false);
  // One-time secrets: component state only, dropped on every step change and on unmount.
  const [courierResult, setCourierResult] = useState<CourierSecretsResponse | null>(null);
  const [keyResult, setKeyResult] = useState<IssueKeyResponse | null>(null);

  const goStep = useCallback((next: StepId) => {
    setCourierResult(null);
    setKeyResult(null);
    setError(null);
    setStep(next);
  }, []);

  const refresh = useCallback(
    async (id: number): Promise<boolean> => {
      setChecklistError(null);
      try {
        setChecklist(await client.checklist(id));
        return true;
      } catch (err) {
        setChecklistError(toStepError(err));
        return false;
      }
    },
    [client],
  );

  const adopt = (id: number) => {
    storeTenant(id);
    setTenantId(id);
  };

  const reset = () => {
    storeTenant(null);
    setTenantId(null);
    setChecklist(null);
    setChecklistError(null);
    goStep('create');
  };

  // Reload resumes at the checklist for the stored tenant.
  useEffect(() => {
    if (tenantId !== null) void refresh(tenantId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** Runs a step request with shared busy/error handling. Returns the result or null on failure. */
  const run = async <R,>(fn: () => Promise<R>): Promise<R | null> => {
    setBusy(true);
    setError(null);
    try {
      return await fn();
    } catch (err) {
      setError(toStepError(err));
      return null;
    } finally {
      setBusy(false);
    }
  };

  const loadExisting = async (id: number) => {
    const result = await run(() => client.checklist(id));
    if (result === null) return;
    adopt(id);
    setChecklist(result);
    setChecklistError(null);
    goStep('checklist');
  };

  const createTenant = async (values: Record<string, string>) => {
    const name = values.name.trim();
    const res = await run(() =>
      client.createTenant({
        shop_domain: values.shop_domain.trim(),
        platform: values.platform === 'salla' ? 'salla' : 'shopify',
        country: values.country,
        currency: values.currency,
        ...(name ? { name } : {}),
      }),
    );
    if (res === null) return;
    adopt(res.tenant_id);
    await refresh(res.tenant_id);
    goStep('meta');
  };

  const storeMeta = async (values: Record<string, string>) => {
    if (tenantId === null) return;
    const code = values.test_event_code.trim();
    const res = await run(() =>
      client.storeMeta(tenantId, {
        meta_dataset_id: values.meta_dataset_id.trim(),
        meta_capi_token: values.meta_capi_token,
        ...(code ? { test_event_code: code } : {}),
      }),
    );
    if (res === null) return;
    await refresh(tenantId);
    goStep('courier');
  };

  const makeCourierSecrets = async (couriers: CourierKind[], rotate: boolean) => {
    if (tenantId === null) return;
    const res = await run(() => client.courierSecrets(tenantId, { couriers, rotate }));
    if (res === null) return;
    setCourierResult(res);
    await refresh(tenantId);
  };

  const issueKey = async () => {
    if (tenantId === null) return;
    const res = await run(() => client.issueApiKey(tenantId));
    if (res === null) return;
    setKeyResult(res);
    await refresh(tenantId);
  };

  const index = STEPS.indexOf(step);

  return (
    <div className="onboarding">
      <nav aria-label={t.stepper}>
        <ol className="stepper">
          {STEPS.map((id, i) => (
            <li key={id}>
              <button
                type="button"
                className="stepper-item"
                aria-current={id === step ? 'step' : undefined}
                disabled={busy || (id !== 'create' && tenantId === null)}
                onClick={() => {
                  if (id === 'checklist' && tenantId !== null) void refresh(tenantId);
                  goStep(id);
                }}
              >
                <span className="stepper-n">{i + 1}</span>
                <span>{t.steps[id]}</span>
              </button>
            </li>
          ))}
        </ol>
      </nav>

      <p className="filter-help" role="status">
        {interpolate(t.stepOf, { n: index + 1, total: STEPS.length })}
        {tenantId !== null ? ` · ${interpolate(t.currentTenant, { id: tenantId })}` : ''}
      </p>

      {step === 'create' ? (
        <CreateStep t={t} busy={busy} error={error} onCreate={createTenant} onLoad={loadExisting} />
      ) : null}
      {step === 'meta' ? <MetaStep t={t} busy={busy} error={error} onSubmit={storeMeta} /> : null}
      {step === 'courier' ? (
        <CourierStep
          t={t}
          busy={busy}
          error={error}
          result={courierResult}
          onSubmit={makeCourierSecrets}
          onContinue={() => goStep('apikey')}
        />
      ) : null}
      {step === 'apikey' ? (
        <ApiKeyStep
          t={t}
          busy={busy}
          error={error}
          result={keyResult}
          onIssue={issueKey}
          onContinue={() => {
            goStep('checklist');
          }}
        />
      ) : null}
      {step === 'checklist' ? (
        <ChecklistStep
          t={t}
          checklist={checklist}
          error={checklistError}
          onRefresh={() => (tenantId !== null ? refresh(tenantId) : Promise.resolve(false))}
        />
      ) : null}

      {tenantId !== null ? (
        <button type="button" className="button" onClick={reset} disabled={busy}>
          {t.switchTenant}
        </button>
      ) : null}
    </div>
  );
}

function ErrorBox({ t, error }: { t: T; error: StepError | null }) {
  if (error === null) return null;
  return (
    <div className="onboarding-error" role="alert">
      <strong>{t.errors.heading}</strong>
      <div>{errorLead(t.errors, error.status)}</div>
      {error.detail ? (
        <div className="onboarding-error-detail" dir="auto">
          {error.detail}
        </div>
      ) : null}
    </div>
  );
}

function StepCard({ title, help, children }: { title: string; help?: string; children: ReactNode }) {
  return (
    <section className="card step-card">
      <h2>{title}</h2>
      {help ? <p className="filter-help">{help}</p> : null}
      {children}
    </section>
  );
}

function Fields({
  specs,
  labels,
  platformLabels,
  values,
  setValues,
}: {
  specs: readonly FieldSpec[];
  labels: Record<string, string>;
  platformLabels: Record<string, string>;
  values: Record<string, string>;
  setValues: (next: Record<string, string>) => void;
}) {
  return (
    <>
      {specs.map((spec) => {
        const id = `onb-${spec.name}`;
        const value = values[spec.name] ?? '';
        const invalid = value !== '' && !spec.valid(value);
        const change = (raw: string) =>
          setValues({ ...values, [spec.name]: spec.transform ? spec.transform(raw) : raw });
        return (
          <div className="field" key={spec.name}>
            <label className="filter-label" htmlFor={id}>
              {labels[spec.name]}
            </label>
            {spec.kind === 'select' ? (
              <select id={id} className="filter-input" value={value} onChange={(e) => change(e.target.value)}>
                {spec.options?.map((option) => (
                  <option key={option} value={option}>
                    {platformLabels[option] ?? option}
                  </option>
                ))}
              </select>
            ) : (
              <input
                id={id}
                className="filter-input"
                type={spec.kind === 'password' ? 'password' : 'text'}
                autoComplete="off"
                autoCapitalize="off"
                spellCheck={false}
                dir="ltr"
                inputMode={spec.inputMode}
                maxLength={spec.maxLength}
                value={value}
                aria-invalid={invalid ? true : undefined}
                onChange={(e) => change(e.target.value)}
              />
            )}
          </div>
        );
      })}
    </>
  );
}

function CreateStep({
  t,
  busy,
  error,
  onCreate,
  onLoad,
}: {
  t: T;
  busy: boolean;
  error: StepError | null;
  onCreate: (values: Record<string, string>) => void;
  onLoad: (id: number) => void;
}) {
  const [values, setValues] = useState(CREATE_DEFAULTS);
  const [existing, setExisting] = useState('');
  const existingValid = /^[1-9]\d*$/.test(existing.trim());
  const submit = (event: FormEvent) => {
    event.preventDefault();
    onCreate(values);
  };
  const submitExisting = (event: FormEvent) => {
    event.preventDefault();
    if (existingValid) onLoad(Number(existing.trim()));
  };
  return (
    <>
      <StepCard title={t.create.title} help={t.create.help}>
        <form className="signin-form" onSubmit={submit}>
          <Fields
            specs={CREATE_FIELDS}
            labels={t.create.fields}
            platformLabels={t.create.platforms}
            values={values}
            setValues={setValues}
          />
          <ErrorBox t={t} error={error} />
          <button type="submit" className="button button-primary" disabled={busy || !formValid(CREATE_FIELDS, values)}>
            {busy ? t.busy : t.create.submit}
          </button>
        </form>
      </StepCard>
      <StepCard title={t.create.existingTitle}>
        <form className="signin-form" onSubmit={submitExisting}>
          <label className="filter-label" htmlFor="onb-existing">
            {t.create.existingLabel}
          </label>
          <input
            id="onb-existing"
            className="filter-input"
            inputMode="numeric"
            autoComplete="off"
            dir="ltr"
            value={existing}
            aria-invalid={existing !== '' && !existingValid ? true : undefined}
            onChange={(e) => setExisting(e.target.value)}
          />
          {existing !== '' && !existingValid ? (
            <div className="filter-error">{t.create.existingInvalid}</div>
          ) : null}
          <button type="submit" className="button" disabled={busy || !existingValid}>
            {t.create.existingSubmit}
          </button>
        </form>
      </StepCard>
    </>
  );
}

function MetaStep({
  t,
  busy,
  error,
  onSubmit,
}: {
  t: T;
  busy: boolean;
  error: StepError | null;
  onSubmit: (values: Record<string, string>) => void;
}) {
  const [values, setValues] = useState(META_DEFAULTS);
  const [sent, setSent] = useState(false);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    const snapshot = values;
    // The token leaves component state as soon as the request is issued; it is never kept or shown again.
    setValues({ ...values, meta_capi_token: '' });
    setSent(true);
    onSubmit(snapshot);
  };
  return (
    <StepCard title={t.meta.title} help={t.meta.help}>
      <form className="signin-form" onSubmit={submit}>
        <Fields
          specs={META_FIELDS}
          labels={t.meta.fields}
          platformLabels={{}}
          values={values}
          setValues={(next) => {
            setSent(false);
            setValues(next);
          }}
        />
        {sent && error !== null ? <div className="filter-help">{t.meta.tokenCleared}</div> : null}
        <ErrorBox t={t} error={error} />
        <button type="submit" className="button button-primary" disabled={busy || !formValid(META_FIELDS, values)}>
          {busy ? t.busy : t.meta.submit}
        </button>
      </form>
    </StepCard>
  );
}

function OnceWarning({ t }: { t: T }) {
  return (
    <p className="once-warning" role="alert">
      {t.secretWarning}
    </p>
  );
}

function CourierStep({
  t,
  busy,
  error,
  result,
  onSubmit,
  onContinue,
}: {
  t: T;
  busy: boolean;
  error: StepError | null;
  result: CourierSecretsResponse | null;
  onSubmit: (couriers: CourierKind[], rotate: boolean) => void;
  onContinue: () => void;
}) {
  const [selected, setSelected] = useState<Record<CourierKind, boolean>>({ bosta: true, oto: true });
  const [rotate, setRotate] = useState(false);
  const couriers = (['bosta', 'oto'] as const).filter((c) => selected[c]);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    onSubmit(couriers, rotate);
  };

  if (result !== null) {
    return (
      <StepCard title={t.courier.resultTitle}>
        <OnceWarning t={t} />
        {(['bosta', 'oto'] as const).map((c) => {
          const item = result.secrets[c];
          if (!item) return null;
          return (
            <fieldset className="once-panel" key={c}>
              <legend>{t.courier[c]}</legend>
              <CopyField label={`${t.courier[c]} · ${t.courier.url}`} value={item.url} t={t} />
              <CopyField label={`${t.courier[c]} · ${t.courier.secret}`} value={item.secret} t={t} />
            </fieldset>
          );
        })}
        <button type="button" className="button button-primary" onClick={onContinue}>
          {t.continue}
        </button>
      </StepCard>
    );
  }

  return (
    <StepCard title={t.courier.title} help={t.courier.help}>
      <form className="signin-form" onSubmit={submit}>
        <div className="check-row">
          {(['bosta', 'oto'] as const).map((c) => (
            <label key={c} className="check-label">
              <input
                type="checkbox"
                checked={selected[c]}
                onChange={(e) => setSelected({ ...selected, [c]: e.target.checked })}
              />
              {t.courier[c]}
            </label>
          ))}
        </div>
        <label className="check-label">
          <input type="checkbox" checked={rotate} onChange={(e) => setRotate(e.target.checked)} />
          {t.courier.rotate}
        </label>
        {rotate ? (
          <p className="once-warning" role="alert">
            {t.courier.rotateWarning}
          </p>
        ) : null}
        {couriers.length === 0 ? <div className="filter-error">{t.courier.noneSelected}</div> : null}
        <ErrorBox t={t} error={error} />
        <button type="submit" className="button button-primary" disabled={busy || couriers.length === 0}>
          {busy ? t.busy : t.courier.submit}
        </button>
      </form>
    </StepCard>
  );
}

function ApiKeyStep({
  t,
  busy,
  error,
  result,
  onIssue,
  onContinue,
}: {
  t: T;
  busy: boolean;
  error: StepError | null;
  result: IssueKeyResponse | null;
  onIssue: () => void;
  onContinue: () => void;
}) {
  if (result !== null) {
    return (
      <StepCard title={t.apikey.resultTitle}>
        <OnceWarning t={t} />
        <div className="once-panel">
          <CopyField label={t.apikey.key} value={result.api_key} t={t} />
          <div className="filter-help" dir="ltr">
            {interpolate(t.apikey.prefix, { prefix: result.key_prefix })}
          </div>
        </div>
        <button type="button" className="button button-primary" onClick={onContinue}>
          {t.continue}
        </button>
      </StepCard>
    );
  }
  return (
    <StepCard title={t.apikey.title} help={t.apikey.help}>
      <ErrorBox t={t} error={error} />
      <button type="button" className="button button-primary" disabled={busy} onClick={onIssue}>
        {busy ? t.busy : t.apikey.submit}
      </button>
    </StepCard>
  );
}

function ChecklistStep({
  t,
  checklist,
  error,
  onRefresh,
}: {
  t: T;
  checklist: ChecklistResponse | null;
  error: StepError | null;
  onRefresh: () => Promise<boolean>;
}) {
  const labels = t.checklist.labels as Record<string, string>;
  return (
    <StepCard title={t.checklist.title}>
      <ErrorBox t={t} error={error} />
      {checklist === null && error === null ? <p>{t.checklist.loading}</p> : null}
      {checklist !== null ? (
        <>
          <p className="filter-help" dir="ltr">
            {interpolate(t.checklist.mode, { mode: checklist.mode })}
          </p>
          <ul className="checklist">
            {checklist.steps.map((s) => (
              <li key={s.key} className="checklist-item" data-status={s.status}>
                <span className="checklist-status">{t.checklist.status[s.status]}</span>
                <div className="checklist-main">
                  <div>{labels[s.key] ?? s.label}</div>
                  <div className="filter-help">
                    {s.required ? t.checklist.required : t.checklist.optional}
                    {s.manual ? ` · ${t.checklist.manual}` : ''}
                  </div>
                  {s.detail && s.key !== 'platform_webhooks' ? (
                    <div className="filter-help" dir="ltr">
                      {s.detail}
                    </div>
                  ) : null}
                </div>
              </li>
            ))}
          </ul>
          {checklist.platform_webhook_urls.length > 0 ? (
            <div className="once-panel">
              <strong>{t.checklist.webhookUrls}</strong>
              {checklist.platform_webhook_urls.map((url) => (
                <CopyField key={url} label={url} value={url} t={t} />
              ))}
            </div>
          ) : null}
          <p className="checklist-ready" data-ready={checklist.ready_for_live}>
            {checklist.ready_for_live ? t.checklist.ready : t.checklist.notReady}
          </p>
          <p className="filter-help">{t.checklist.liveNote}</p>
        </>
      ) : null}
      <button type="button" className="button" onClick={() => void onRefresh()}>
        {t.checklist.refresh}
      </button>
    </StepCard>
  );
}
