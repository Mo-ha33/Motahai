import { dictionaries, type Lang } from '../i18n';
import { useFilters } from './FiltersContext';

export function TenantFilter({ lang }: { lang: Lang }) {
  const t = dictionaries[lang].filters;
  const { tenantInput, tenantId, tenantLocked, setTenantInput } = useFilters();
  if (tenantLocked) return null;
  const invalid = tenantInput.trim() !== '' && tenantId === null;
  return (
    <div className="filter-card">
      <label className="filter-label" htmlFor="filter-tenant">
        {t.tenantLabel}
      </label>
      <input
        id="filter-tenant"
        className="filter-input"
        type="text"
        inputMode="numeric"
        autoComplete="off"
        value={tenantInput}
        aria-invalid={invalid || undefined}
        aria-describedby="filter-tenant-help"
        onChange={(event) => setTenantInput(event.target.value)}
      />
      <div id="filter-tenant-help" className="filter-help" role={invalid ? 'alert' : undefined}>
        {invalid ? t.tenantInvalid : t.tenantHelp}
      </div>
    </div>
  );
}
