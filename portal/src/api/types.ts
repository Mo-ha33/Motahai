/** Exact shapes of the Stats API (src/ameen_workforce/stats_routes.py). */

export const STATS_SECTIONS = [
  'week-orders',
  'cohort-delivery',
  'refused-cod',
  'signal-health',
  'creatives',
] as const;
export type StatsSection = (typeof STATS_SECTIONS)[number];

/** Date window sent to the API: ISO dates (YYYY-MM-DD), half-open [start, end), UTC. */
export interface StatsWindow {
  start: string;
  end: string;
}

export interface WeekOrders {
  placed: number;
  cod: number;
  confirmed: number;
}

export interface CohortBucket {
  orders: number;
  delivered: number;
  delivered_value: number;
  gross_value: number;
}

export interface CohortDelivery {
  all: CohortBucket;
  ad_driven: CohortBucket;
}

export interface RefusedCod {
  count: number;
  value: number;
}

export interface SignalHealth {
  confirmed_sent: number;
  confirmed_shadow: number;
  delivered_sent: number;
  delivered_shadow: number;
  total_events: number;
  flagged_events: number;
  late_delivery: number;
}

export interface CreativeRow {
  ad_id: string;
  orders: number;
  delivered: number;
  delivery_rate: number;
  delivered_value: number;
}

export interface SectionDataMap {
  'week-orders': WeekOrders;
  'cohort-delivery': CohortDelivery;
  'refused-cod': RefusedCod;
  'signal-health': SignalHealth;
  creatives: CreativeRow[];
}

/** `summary.data`: every section keyed by its endpoint name. */
export type StatsSummaryData = SectionDataMap;

export interface StatsEnvelope<D = StatsSummaryData> {
  tenant_id: number;
  /** The tenant's ISO 4217 currency code; absent on older API versions. */
  currency?: string;
  /** Report window as full ISO datetimes. */
  window: StatsWindow;
  /** Present on cohort-based sections and on summary (window shifted back 7 days). */
  cohort_window?: StatsWindow;
  data: D;
}

export type HealthStatus = 'healthy' | 'degraded' | 'critical' | 'insufficient_data';

/** Evidence counts of one penalty (keys vary by penalty name; see health_score.py). */
export type HealthEvidence = Record<string, number | boolean | null>;

export interface HealthPenalty {
  /** flagged_events, late_delivery, scheduler_stale, job_failures, incidents_*, webhooks_*; unknown names are possible. */
  name: string;
  points: number;
  evidence: HealthEvidence;
}

/** `health-score.data`. `score` is null (never 0 or 100) when status is insufficient_data. */
export interface HealthScoreData {
  score: number | null;
  status: HealthStatus;
  penalties: HealthPenalty[];
}

/** Dashboard data sources: each is one endpoint under /v1/tenants/{id}/stats/. */
export type StatsSourceId = 'summary' | 'health-score';

export interface SourceDataMap {
  summary: StatsSummaryData;
  'health-score': HealthScoreData;
}

/** Source id -> its response envelope. */
export type SourceEnvelopeMap = { [S in StatsSourceId]: StatsEnvelope<SourceDataMap[S]> };

/** `GET /v1/tenant/me` (tenant key only). */
export interface TenantProfile {
  tenant_id: number;
  name: string;
  currency: string;
  country: string;
  mode: string;
  platform: string;
}

/* Operator onboarding (`/v1/operator/onboarding`). Mirrors src/ameen_workforce/onboarding_routes.py. */

export type OnboardingPlatform = 'shopify' | 'salla';
export type CourierKind = 'bosta' | 'oto';

export interface CreateTenantRequest {
  shop_domain: string;
  platform: OnboardingPlatform;
  country: string;
  currency: string;
  name?: string;
}

export interface CreateTenantResponse {
  tenant_id: number;
  shop_domain: string;
  platform: OnboardingPlatform;
  country: string;
  currency: string;
  mode: string;
  live_sending: boolean;
}

export interface MetaRequest {
  meta_dataset_id: string;
  meta_capi_token: string;
  test_event_code?: string;
}

export interface MetaResponse {
  tenant_id: number;
  meta_dataset_id: string;
  /** A fixed status string, never the token. */
  meta_capi_token: string;
  test_event_code_set: boolean;
}

export interface CourierSecretsRequest {
  couriers?: CourierKind[];
  rotate?: boolean;
}

export interface CourierSecretsResponse {
  tenant_id: number;
  /** Returned ONCE. */
  secrets: Partial<Record<CourierKind, { url: string; secret: string }>>;
  note: string;
}

export interface IssueKeyResponse {
  tenant_id: number;
  key_id: number;
  key_prefix: string;
  scopes: string | string[];
  /** Returned ONCE. */
  api_key: string;
}

export type ChecklistStatus = 'done' | 'manual' | 'missing';

export interface ChecklistStep {
  key: string;
  label: string;
  required: boolean;
  manual: boolean;
  status: ChecklistStatus;
  detail: string;
}

export interface ChecklistResponse {
  tenant_id: number;
  mode: string;
  steps: ChecklistStep[];
  platform_webhook_urls: string[];
  ready_for_live: boolean;
  note: string;
}

export const AUDIENCE_LIST_NAMES = ['exclude_refusers', 'seed_delivered_buyers'] as const;
export type AudienceListName = (typeof AUDIENCE_LIST_NAMES)[number];

/** Counts and timestamps only: the API never returns hashes or row contents in the status. */
export interface AudienceListStatus {
  name: AudienceListName;
  rows: number | null;
  updated_at: string | null;
  size_bytes: number | null;
}

export interface AudienceSchedule {
  job_name: string;
  last_run_at: string | null;
  last_run_ok: boolean | null;
  last_error_type: string | null;
  next_due_at: string | null;
}

export interface AudiencesStatus {
  tenant_id: number;
  min_list_rows: number;
  lists: AudienceListStatus[];
  schedule: AudienceSchedule;
}
