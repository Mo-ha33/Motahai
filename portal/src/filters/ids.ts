export const FILTER_IDS = ['tenant', 'date-range'] as const;
export type FilterId = (typeof FILTER_IDS)[number];
