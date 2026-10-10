import { dictionaries, interpolate, type Lang } from '../i18n';
import { useFilters } from './FiltersContext';
import { MAX_RANGE_DAYS, PRESET_DAYS } from './range';

export function DateRangeFilter({ lang }: { lang: Lang }) {
  const t = dictionaries[lang].filters;
  const { range, rangeError, setPreset, setCustomRange } = useFilters();
  const message =
    rangeError === 'order'
      ? t.errorOrder
      : rangeError === 'span'
        ? interpolate(t.errorSpan, { n: MAX_RANGE_DAYS })
        : rangeError === 'invalid'
          ? t.errorInvalid
          : null;
  return (
    <div className="filter-card">
      <div className="filter-label" id="filter-range-label">
        {t.rangeLabel}
      </div>
      <div className="preset-row" role="group" aria-labelledby="filter-range-label">
        {PRESET_DAYS.map((days) => (
          <button
            key={days}
            type="button"
            className="chip"
            aria-pressed={range.preset === days}
            onClick={() => setPreset(days)}
          >
            {interpolate(t.presetDays, { n: days })}
          </button>
        ))}
        <button
          type="button"
          className="chip"
          aria-pressed={range.preset === 'custom'}
          onClick={() => setPreset('custom')}
        >
          {t.custom}
        </button>
      </div>
      <div className="date-row">
        <label className="filter-sublabel">
          {t.start}
          <input
            className="filter-input"
            type="date"
            value={range.start}
            onChange={(event) => setCustomRange(event.target.value, range.end)}
          />
        </label>
        <label className="filter-sublabel">
          {t.end}
          <input
            className="filter-input"
            type="date"
            value={range.end}
            onChange={(event) => setCustomRange(range.start, event.target.value)}
          />
        </label>
      </div>
      {message ? (
        <div className="filter-error" role="alert">
          {message}
        </div>
      ) : null}
    </div>
  );
}
