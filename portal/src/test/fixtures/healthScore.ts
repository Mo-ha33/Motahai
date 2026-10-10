import type { HealthScoreData, StatsEnvelope } from '../../api/types';

const statsWindow = { start: '2026-10-03T00:00:00+00:00', end: '2026-10-10T00:00:00+00:00' };

/** Degraded: flagged events 18 + one open error incident 10 = 28 deducted, score 72. Penalties deliberately unsorted. */
export const degradedHealthData: HealthScoreData = {
  score: 72,
  status: 'degraded',
  penalties: [
    { name: 'incidents_error', points: 10, evidence: { open: 1 } },
    { name: 'flagged_events', points: 18, evidence: { flagged: 12, total: 340 } },
  ],
};

/** Healthy: two late deliveries cost 4, score 96. */
export const healthyHealthData: HealthScoreData = {
  score: 96,
  status: 'healthy',
  penalties: [{ name: 'late_delivery', points: 4, evidence: { count: 2 } }],
};

/** Critical: job failures 15 + two critical incidents 40 + stale scheduler 15 = 70 deducted, score 30. */
export const criticalHealthData: HealthScoreData = {
  score: 30,
  status: 'critical',
  penalties: [
    { name: 'job_failures', points: 15, evidence: { count: 5 } },
    { name: 'incidents_critical', points: 40, evidence: { open: 2 } },
    { name: 'scheduler_stale', points: 15, evidence: { last_success_found: true, age_seconds: 7200 } },
  ],
};

export const noDeductionsHealthData: HealthScoreData = { score: 100, status: 'healthy', penalties: [] };

export const insufficientHealthData: HealthScoreData = { score: null, status: 'insufficient_data', penalties: [] };

export function healthEnvelope(data: HealthScoreData): StatsEnvelope<HealthScoreData> {
  return { tenant_id: 7, window: statsWindow, data };
}

export const healthScoreEnvelope = healthEnvelope(degradedHealthData);
