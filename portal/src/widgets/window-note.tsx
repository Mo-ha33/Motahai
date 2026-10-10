import { formatDate } from '../format';
import { dictionaries, interpolate } from '../i18n';
import type { WidgetProps } from './registry';
import { WidgetFrame } from './shared';

export function WindowNoteWidget({ envelope, instance, lang }: WidgetProps<Record<string, never>>) {
  const t = dictionaries[lang];
  const range = (w: { start: string; end: string }) =>
    interpolate(t.windowNote.range, {
      start: formatDate(w.start, lang),
      end: formatDate(w.end, lang),
    });
  return (
    <WidgetFrame instance={instance} lang={lang}>
      <dl className="window-note">
        <div>
          <dt>{t.windowNote.report}</dt>
          <dd>{range(envelope.window)}</dd>
        </div>
        {envelope.cohort_window ? (
          <div>
            <dt>{t.windowNote.cohort}</dt>
            <dd>{range(envelope.cohort_window)}</dd>
          </div>
        ) : null}
      </dl>
      <p className="widget-note">{t.windowNote.explain}</p>
    </WidgetFrame>
  );
}
