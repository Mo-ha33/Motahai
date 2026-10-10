export type Lang = 'ar' | 'en';

export const DEFAULT_LANG: Lang = 'ar';

type Widen<T> = T extends string ? string : { [K in keyof T]: Widen<T[K]> };

const ar = {
  productName: 'Motahai',
  language: 'English',
  nav: {
    signal: 'صحة الإشارة',
    roas: 'العائد الإعلاني المُسلَّم',
    audiences: 'الجماهير',
    onboarding: 'الإعداد',
  },
  pages: {
    signal: { title: 'صحة الإشارة' },
    roas: { title: 'العائد الإعلاني المُسلَّم (ROAS)' },
    audiences: { title: 'الجماهير' },
    onboarding: { title: 'الإعداد' },
  },
  emptyState: {
    heading: 'لا توجد بيانات بعد',
    body: 'ستظهر البيانات هنا فور ربط الواجهة البرمجية (API).',
  },
  notFound: {
    heading: 'الصفحة غير موجودة',
    body: 'اختر قسمًا من القائمة الجانبية.',
  },
  theme: {
    label: 'المظهر',
    auto: 'تلقائي',
    light: 'فاتح',
    dark: 'داكن',
  },
  filters: {
    tenantLabel: 'رقم المتجر (Tenant)',
    tenantHelp: 'أدخل الرقم التعريفي للمتجر.',
    tenantInvalid: 'أدخل رقمًا صحيحًا موجبًا.',
    rangeLabel: 'الفترة الزمنية',
    presetDays: 'آخر {n} يومًا',
    custom: 'مخصص',
    start: 'من',
    end: 'إلى (غير شامل)',
    errorOrder: 'يجب أن يكون تاريخ النهاية بعد تاريخ البداية.',
    errorSpan: 'الحد الأقصى للفترة {n} يومًا.',
    errorInvalid: 'أدخل تاريخين صحيحين.',
  },
  states: {
    chooseTenant: 'اختر متجرًا',
    chooseTenantBody: 'أدخل رقم المتجر أعلاه لعرض الأرقام.',
    fixRange: 'الفترة الزمنية غير صالحة',
    fixRangeBody: 'صحّح الفترة أعلاه لعرض الأرقام.',
    loading: 'جارٍ التحميل…',
    retry: 'إعادة المحاولة',
    errorHeading: 'تعذّر تحميل البيانات',
    error401:
      'لا يوجد مفتاح مشغّل مضبوط في وسيط الواجهة البرمجية (API proxy). راجع portal/README.',
    error404: 'المتجر غير معروف. تحقق من رقم المتجر.',
    error0: 'تعذّر الوصول إلى الواجهة البرمجية (API).',
    errorOther: 'حدث خطأ غير متوقع ({status}).',
  },
  widgetErrors: {
    unknownType: 'نوع عنصر غير معروف: {type}',
  },
  noData: 'لا توجد بيانات في هذه الفترة',
  creatives: {
    empty: 'لم يصل أي إعلان إلى الحد الأدنى من الطلبات خلال هذه الفترة.',
    columns: {
      ad_id: 'معرّف الإعلان',
      orders: 'الطلبات',
      delivered: 'المُسلَّم',
      delivery_rate: 'نسبة التسليم',
      delivered_value: 'قيمة المُسلَّم',
    },
  },
  windowNote: {
    report: 'فترة التقرير',
    cohort: 'فترة الدفعة (Cohort)',
    explain:
      'تُحسب أرقام التسليم والاسترجاع على طلبات الفترة نفسها مُزاحة سبعة أيام للخلف، لإتاحة وقت كافٍ للتسليم. التواريخ بتوقيت UTC.',
    range: '{start} ← {end}',
  },
  funnel: {
    stepRate: '{pct} من الخطوة السابقة',
  },
  widgetTitles: {
    window: 'نافذة البيانات',
    orders: 'الطلبات',
    cohortFunnel: 'مسار التسليم',
    deliveryQuality: 'جودة التسليم',
    valueGap: 'القيمة الإجمالية مقابل المُسلَّمة',
    refusedCod: 'رفض الدفع عند الاستلام',
    creatives: 'الإعلانات',
    signalOverview: 'نظرة عامة على الإشارة',
    confirmedEvents: 'أحداث التأكيد: المرسلة مقابل الظل',
    deliveredEvents: 'أحداث التسليم: المرسلة مقابل الظل',
  },
  metrics: {
    orders_placed: 'الطلبات المُنشأة',
    orders_cod: 'طلبات الدفع عند الاستلام',
    orders_confirmed: 'الطلبات المؤكدة',
    confirmation_rate: 'نسبة التأكيد',
    cod_share: 'حصة الدفع عند الاستلام',
    cohort_orders: 'طلبات الدفعة',
    cohort_delivered: 'المُسلَّم من الدفعة',
    delivery_rate: 'نسبة التسليم',
    ad_orders: 'طلبات الإعلانات',
    ad_delivered: 'المُسلَّم من الإعلانات',
    ad_delivery_rate: 'نسبة تسليم الإعلانات',
    delivered_value: 'قيمة المُسلَّم',
    gross_value: 'القيمة الإجمالية',
    ad_delivered_value: 'قيمة المُسلَّم من الإعلانات',
    value_inflation: 'تضخّم القيمة',
    refused_count: 'طلبات مرفوضة',
    refused_value: 'قيمة المرفوض',
    signal_events: 'إجمالي الأحداث',
    confirmed_sent: 'تأكيد مُرسَل',
    confirmed_shadow: 'تأكيد (ظل)',
    delivered_sent: 'تسليم مُرسَل',
    delivered_shadow: 'تسليم (ظل)',
    flagged_events: 'أحداث مُعلَّمة',
    flagged_share: 'نسبة الأحداث المُعلَّمة',
    late_delivery: 'تسليم متأخر',
  },
  metricHints: {
    confirmation_rate: 'المؤكدة ÷ المُنشأة',
    cod_share: 'الدفع عند الاستلام ÷ المُنشأة',
    cohort_orders: 'طلبات الفترة مُزاحة سبعة أيام للخلف',
    delivery_rate: 'المُسلَّم ÷ طلبات الدفعة',
    ad_delivery_rate: 'المُسلَّم ÷ الطلبات القادمة من الإعلانات',
    value_inflation: 'القيمة الإجمالية ÷ قيمة المُسلَّم',
    refused_count: 'طلبات دفع عند الاستلام تم رفضها',
    flagged_share: 'المُعلَّمة ÷ إجمالي الأحداث',
    late_delivery: 'أحداث تسليم وصلت متأخرة',
  },
} as const;

export type Dictionary = Widen<typeof ar>;

const en: Dictionary = {
  productName: 'Motahai',
  language: 'العربية',
  nav: {
    signal: 'Signal health',
    roas: 'Delivered ROAS',
    audiences: 'Audiences',
    onboarding: 'Onboarding',
  },
  pages: {
    signal: { title: 'Signal health' },
    roas: { title: 'Delivered ROAS' },
    audiences: { title: 'Audiences' },
    onboarding: { title: 'Onboarding' },
  },
  emptyState: {
    heading: 'No data yet',
    body: 'Data will appear here once the API is connected.',
  },
  notFound: {
    heading: 'Page not found',
    body: 'Choose a section from the sidebar.',
  },
  theme: {
    label: 'Theme',
    auto: 'Auto',
    light: 'Light',
    dark: 'Dark',
  },
  filters: {
    tenantLabel: 'Tenant ID',
    tenantHelp: 'Enter the numeric tenant id.',
    tenantInvalid: 'Enter a positive whole number.',
    rangeLabel: 'Date range',
    presetDays: 'Last {n} days',
    custom: 'Custom',
    start: 'From',
    end: 'To (exclusive)',
    errorOrder: 'The end date must be after the start date.',
    errorSpan: 'The range can be at most {n} days.',
    errorInvalid: 'Enter two valid dates.',
  },
  states: {
    chooseTenant: 'Choose a tenant',
    chooseTenantBody: 'Enter a tenant id above to see the numbers.',
    fixRange: 'Invalid date range',
    fixRangeBody: 'Fix the date range above to see the numbers.',
    loading: 'Loading…',
    retry: 'Retry',
    errorHeading: 'Could not load the data',
    error401:
      'The API proxy has no operator key configured. See portal/README.',
    error404: 'Unknown tenant. Check the tenant id.',
    error0: 'Cannot reach the API.',
    errorOther: 'Unexpected error ({status}).',
  },
  widgetErrors: {
    unknownType: 'Unknown widget type: {type}',
  },
  noData: 'No data in this window',
  creatives: {
    empty: 'No creative reached the minimum number of orders in this window.',
    columns: {
      ad_id: 'Ad ID',
      orders: 'Orders',
      delivered: 'Delivered',
      delivery_rate: 'Delivery rate',
      delivered_value: 'Delivered value',
    },
  },
  windowNote: {
    report: 'Report window',
    cohort: 'Cohort window',
    explain:
      'Delivery and refusal numbers use the same window shifted back seven days, so orders have had time to be delivered. Dates are UTC.',
    range: '{start} → {end}',
  },
  funnel: {
    stepRate: '{pct} of previous step',
  },
  widgetTitles: {
    window: 'Data window',
    orders: 'Orders',
    cohortFunnel: 'Delivery funnel',
    deliveryQuality: 'Delivery quality',
    valueGap: 'Gross vs delivered value',
    refusedCod: 'Refused COD',
    creatives: 'Creatives',
    signalOverview: 'Signal overview',
    confirmedEvents: 'Confirmed events: sent vs shadow',
    deliveredEvents: 'Delivered events: sent vs shadow',
  },
  metrics: {
    orders_placed: 'Orders placed',
    orders_cod: 'COD orders',
    orders_confirmed: 'Confirmed orders',
    confirmation_rate: 'Confirmation rate',
    cod_share: 'COD share',
    cohort_orders: 'Cohort orders',
    cohort_delivered: 'Cohort delivered',
    delivery_rate: 'Delivery rate',
    ad_orders: 'Ad-driven orders',
    ad_delivered: 'Ad-driven delivered',
    ad_delivery_rate: 'Ad-driven delivery rate',
    delivered_value: 'Delivered value',
    gross_value: 'Gross value',
    ad_delivered_value: 'Ad-driven delivered value',
    value_inflation: 'Value inflation',
    refused_count: 'Refused orders',
    refused_value: 'Refused value',
    signal_events: 'Total events',
    confirmed_sent: 'Confirmed sent',
    confirmed_shadow: 'Confirmed shadow',
    delivered_sent: 'Delivered sent',
    delivered_shadow: 'Delivered shadow',
    flagged_events: 'Flagged events',
    flagged_share: 'Flagged share',
    late_delivery: 'Late delivery',
  },
  metricHints: {
    confirmation_rate: 'Confirmed ÷ placed',
    cod_share: 'COD ÷ placed',
    cohort_orders: 'Orders from the window shifted back seven days',
    delivery_rate: 'Delivered ÷ cohort orders',
    ad_delivery_rate: 'Delivered ÷ ad-driven orders',
    value_inflation: 'Gross value ÷ delivered value',
    refused_count: 'COD orders that were refused',
    flagged_share: 'Flagged ÷ total events',
    late_delivery: 'Delivery events that arrived late',
  },
};

export const dictionaries: { ar: Dictionary; en: Dictionary } = { ar, en };

export type WidgetTitleKey = keyof Dictionary['widgetTitles'];

/** Replaces `{name}` placeholders. */
export function interpolate(template: string, values: Record<string, string | number>): string {
  return template.replace(/\{(\w+)\}/g, (match, key: string) =>
    key in values ? String(values[key]) : match,
  );
}

export function directionFor(lang: Lang): 'rtl' | 'ltr' {
  return lang === 'ar' ? 'rtl' : 'ltr';
}

const STORAGE_KEY = 'motahai.lang';

export function readStoredLang(): Lang {
  try {
    const value = window.localStorage.getItem(STORAGE_KEY);
    if (value === 'ar' || value === 'en') return value;
  } catch {
    // Storage may be blocked (private mode, disabled site data); fall back to default.
  }
  return DEFAULT_LANG;
}

export function storeLang(lang: Lang): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, lang);
  } catch {
    // Non-fatal: the choice simply won't persist.
  }
}

export function applyLangToDocument(lang: Lang): void {
  const root = document.documentElement;
  root.lang = lang;
  root.dir = directionFor(lang);
}
