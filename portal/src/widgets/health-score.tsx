import type { HealthPenalty, HealthScoreData } from '../api/types';
import { localeFor } from '../format';
import { dictionaries, interpolate, type Dictionary, type Lang } from '../i18n';
import type { WidgetProps } from './registry';
import { WidgetFrame } from './shared';

export interface HealthScoreOptions {
  /** Show the deductions list under the gauge. Default true. */
  showPenalties?: boolean;
}

type HealthT = Dictionary['healthScore'];
type Plural = { one: string; other: string };

const STATUS_CLASS = { healthy: 'positive', degraded: 'warning', critical: 'negative' } as const;

function num(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function plural(forms: Plural, n: number, values: Record<string, string | number>): string {
  return interpolate(n === 1 ? forms.one : forms.other, values);
}

function duration(seconds: number, t: HealthT, fmt: (n: number) => string): string {
  const minutes = Math.max(1, Math.round(seconds / 60));
  if (minutes < 60) return interpolate(t.evidence.minutes, { n: fmt(minutes) });
  const hours = Math.round(minutes / 60);
  if (hours < 48) return interpolate(t.evidence.hours, { n: fmt(hours) });
  return interpolate(t.evidence.days, { n: fmt(Math.round(hours / 24)) });
}

/** Short localized evidence line from the penalty's counts; null when the evidence is missing or unknown. */
export function evidenceLine(penalty: HealthPenalty, lang: Lang): string | null {
  const t = dictionaries[lang].healthScore;
  const nf = new Intl.NumberFormat(localeFor(lang), { maximumFractionDigits: 0 });
  const fmt = (n: number) => nf.format(n);
  const ev = penalty.evidence ?? {};
  const counted = (forms: Plural, key: string): string | null => {
    const n = num(ev[key]);
    return n === null ? null : plural(forms, n, { [key]: fmt(n) });
  };
  switch (penalty.name) {
    case 'flagged_events': {
      const flagged = num(ev.flagged);
      const total = num(ev.total);
      return flagged === null || total === null
        ? null
        : interpolate(t.evidence.flagged_events, { flagged: fmt(flagged), total: fmt(total) });
    }
    case 'late_delivery':
      return counted(t.evidence.late_delivery, 'count');
    case 'scheduler_stale': {
      const age = num(ev.age_seconds);
      if (ev.last_success_found === false || age === null) return t.evidence.scheduler_stale_none;
      return interpolate(t.evidence.scheduler_stale_age, { age: duration(age, t, fmt) });
    }
    case 'job_failures':
      return counted(t.evidence.job_failures, 'count');
    case 'incidents_critical':
    case 'incidents_error':
    case 'incidents_warning':
      return counted(t.evidence.incidents, 'open');
    case 'webhooks_dead':
      return counted(t.evidence.webhooks_dead, 'count');
    case 'webhooks_stuck':
      return counted(t.evidence.webhooks_stuck, 'count');
    default:
      return null;
  }
}

function penaltyLabel(name: string, t: HealthT): string {
  const labels = t.penalties as Record<string, string>;
  return Object.prototype.hasOwnProperty.call(labels, name) ? labels[name] : name; // forward compatible
}

function statusLabel(status: string, t: HealthT): string {
  const labels = t.status as Record<string, string>;
  return Object.prototype.hasOwnProperty.call(labels, status) ? labels[status] : status;
}

/** Semicircle gauge; `score` null draws the empty track only. Colours come from tokens via CSS classes. */
function Gauge({ score, status, label }: { score: number | null; status: string; label: string }) {
  const tone = STATUS_CLASS[status as keyof typeof STATUS_CLASS];
  return (
    <svg
      className="health-gauge"
      viewBox="0 0 200 112"
      role="img"
      aria-label={label}
      data-tone={tone ?? 'none'}
    >
      <path className="health-gauge-track" d="M 20 100 A 80 80 0 0 1 180 100" pathLength={100} />
      {score !== null && score > 0 ? (
        <path
          className="health-gauge-fill"
          d="M 20 100 A 80 80 0 0 1 180 100"
          pathLength={100}
          strokeDasharray={`${Math.min(100, score)} 100`}
        />
      ) : null}
    </svg>
  );
}

export function HealthScoreWidget({
  data,
  instance,
  lang,
}: WidgetProps<HealthScoreOptions, HealthScoreData>) {
  const t = dictionaries[lang].healthScore;
  const nf = new Intl.NumberFormat(localeFor(lang), { maximumFractionDigits: 0 });
  const showPenalties = instance.options?.showPenalties !== false;
  const insufficient = data.status === 'insufficient_data' || data.score === null;
  const score = insufficient ? null : data.score;
  const penalties = [...(data.penalties ?? [])].sort((a, b) => b.points - a.points);
  const tone = STATUS_CLASS[data.status as keyof typeof STATUS_CLASS];

  return (
    <WidgetFrame instance={instance} lang={lang}>
      <div className="health-summary">
        <div className="health-gauge-wrap">
          <Gauge
            score={score}
            status={data.status}
            label={score === null ? t.status.insufficient_data : interpolate(t.gaugeLabel, { score: nf.format(score) })}
          />
          {score !== null ? (
            <div className="health-score-text">
              <span className="health-score-value">{nf.format(score)}</span>
              <span className="health-score-max">{t.outOf}</span>
            </div>
          ) : null}
        </div>
        <div className="health-status">
          <span className="health-chip" data-tone={tone ?? 'none'}>
            {statusLabel(data.status, t)}
          </span>
          {insufficient ? <p className="widget-note health-insufficient">{t.insufficient}</p> : null}
        </div>
      </div>
      {showPenalties && !insufficient ? (
        <div className="health-penalties">
          <h3 className="health-penalties-title">{t.penaltiesHeading}</h3>
          {penalties.length === 0 ? (
            <p className="widget-note">{t.noDeductions}</p>
          ) : (
            <ul className="health-penalty-list">
              {penalties.map((p) => {
                const line = evidenceLine(p, lang);
                return (
                  <li key={p.name} className="health-penalty">
                    <div className="health-penalty-main">
                      <span className="health-penalty-label">{penaltyLabel(p.name, t)}</span>
                      {line ? <span className="health-penalty-evidence">{line}</span> : null}
                    </div>
                    <span className="health-penalty-points">
                      <bdi dir="ltr">{interpolate(t.points, { n: nf.format(p.points) })}</bdi>
                    </span>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      ) : null}
    </WidgetFrame>
  );
}
