import type { ComponentType } from 'react';
import type { Lang } from '../i18n';
import { DateRangeFilter } from './DateRangeFilter';
import type { FilterId } from './ids';
import { TenantFilter } from './TenantFilter';

export interface FilterDef {
  id: FilterId;
  Card: ComponentType<{ lang: Lang }>;
}

/** Add a filter: extend FILTER_IDS in ./ids, build a card, and register it here. */
export const filterRegistry: Record<FilterId, FilterDef> = {
  tenant: { id: 'tenant', Card: TenantFilter },
  'date-range': { id: 'date-range', Card: DateRangeFilter },
};

export function getFilter(id: FilterId): FilterDef {
  const def = filterRegistry[id];
  if (!def) throw new Error(`Unknown filter id: ${id}`);
  return def;
}
