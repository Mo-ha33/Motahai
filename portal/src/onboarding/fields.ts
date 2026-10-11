/** Config-driven form fields for the wizard steps. Labels live in the i18n dictionary under the same names. */
export interface FieldSpec {
  name: string;
  kind: 'text' | 'password' | 'select';
  required: boolean;
  options?: readonly string[];
  /** Applied on every keystroke. */
  transform?: (value: string) => string;
  valid: (value: string) => boolean;
  inputMode?: 'numeric' | 'text';
  /** Secret fields: autocomplete off, cleared from state right after submit. */
  secret?: boolean;
  maxLength?: number;
}

const upper = (v: string) => v.toUpperCase();

export const CREATE_FIELDS: readonly FieldSpec[] = [
  { name: 'shop_domain', kind: 'text', required: true, valid: (v) => v.trim().length >= 3, maxLength: 255 },
  { name: 'platform', kind: 'select', required: true, options: ['shopify', 'salla'], valid: (v) => v === 'shopify' || v === 'salla' },
  { name: 'country', kind: 'text', required: true, transform: upper, valid: (v) => /^[A-Z]{2}$/.test(v), maxLength: 2 },
  { name: 'currency', kind: 'text', required: true, transform: upper, valid: (v) => /^[A-Z]{3}$/.test(v), maxLength: 3 },
  { name: 'name', kind: 'text', required: false, valid: () => true, maxLength: 200 },
];

export const META_FIELDS: readonly FieldSpec[] = [
  { name: 'meta_dataset_id', kind: 'text', required: true, inputMode: 'numeric', valid: (v) => /^\d{5,32}$/.test(v.trim()), maxLength: 32 },
  { name: 'meta_capi_token', kind: 'password', required: true, secret: true, valid: (v) => v.trim().length >= 10, maxLength: 1024 },
  { name: 'test_event_code', kind: 'text', required: false, valid: () => true, maxLength: 64 },
];

export const CREATE_DEFAULTS: Record<string, string> = {
  shop_domain: '',
  platform: 'shopify',
  country: '',
  currency: '',
  name: '',
};

export const META_DEFAULTS: Record<string, string> = {
  meta_dataset_id: '',
  meta_capi_token: '',
  test_event_code: '',
};

export function formValid(fields: readonly FieldSpec[], values: Record<string, string>): boolean {
  return fields.every((f) => (!f.required && (values[f.name] ?? '') === '') || f.valid(values[f.name] ?? ''));
}
