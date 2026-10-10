import type { AudiencesStatus } from '../../api/types';

/** 64 lowercase hex chars: the shape of a SHA-256 digest. Used to prove the page never renders hashes. */
export const HASH_A = '0123456789abcdef'.repeat(4);
export const HASH_B = 'fedcba9876543210'.repeat(4);

/** A downloaded list as the server would send it: hashed identifiers only. */
export const HASHED_CSV = `phone,email\n${HASH_A},${HASH_B}\n${HASH_B},\n`;

export const AUDIENCES_STATUS: AudiencesStatus = {
  tenant_id: 7,
  min_list_rows: 100,
  lists: [
    { name: 'exclude_refusers', rows: 2, updated_at: '2026-10-04T10:00:00+00:00', size_bytes: 120 },
    { name: 'seed_delivered_buyers', rows: 100, updated_at: '2026-10-04T10:00:00+00:00', size_bytes: 4000 },
  ],
  schedule: {
    job_name: 'export_weekly_audiences',
    last_run_at: '2026-10-04T10:00:00+00:00',
    last_run_ok: true,
    last_error_type: null,
    next_due_at: '2026-10-11T10:00:00+00:00',
  },
};
