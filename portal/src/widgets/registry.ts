import type { ComponentType } from 'react';
import type { StatsEnvelope, StatsSummaryData } from '../api/types';
import type { WidgetInstance } from '../config/dashboard';
import type { Lang } from '../i18n';

/** `D` is the data shape of the widget's own source (summary unless the instance declares `source`). */
export interface WidgetProps<O = unknown, D = StatsSummaryData> {
  /** `envelope.data` of the widget's own source. */
  data: D;
  /** Envelope of the widget's own source. */
  envelope: StatsEnvelope<D>;
  instance: WidgetInstance<O>;
  lang: Lang;
}

/* A widget's data shape depends on its source, which the registry cannot know: `any` keeps lookups usable. */
/* eslint-disable @typescript-eslint/no-explicit-any */
const widgets = new Map<string, ComponentType<WidgetProps<never>>>();

export function registerWidget<O, D = StatsSummaryData>(
  type: string,
  component: ComponentType<WidgetProps<O, D>>,
): void {
  widgets.set(type, component as unknown as ComponentType<WidgetProps<never>>);
}

export function getWidget(type: string): ComponentType<WidgetProps<unknown, any>> | undefined {
  return widgets.get(type) as unknown as ComponentType<WidgetProps<unknown, any>> | undefined;
}
