import type { ComponentType } from 'react';
import type { StatsEnvelope, StatsSummaryData } from '../api/types';
import type { WidgetInstance } from '../config/dashboard';
import type { Lang } from '../i18n';

export interface WidgetProps<O = unknown> {
  data: StatsSummaryData;
  envelope: StatsEnvelope;
  instance: WidgetInstance<O>;
  lang: Lang;
}

const widgets = new Map<string, ComponentType<WidgetProps<never>>>();

export function registerWidget<O>(type: string, component: ComponentType<WidgetProps<O>>): void {
  widgets.set(type, component as unknown as ComponentType<WidgetProps<never>>);
}

export function getWidget(type: string): ComponentType<WidgetProps<unknown>> | undefined {
  return widgets.get(type) as unknown as ComponentType<WidgetProps<unknown>> | undefined;
}
