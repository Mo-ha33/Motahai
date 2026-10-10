import { CompareBarsWidget } from './compare-bars';
import { CreativesTableWidget } from './creatives-table';
import { FunnelWidget } from './funnel';
import { KpiGroupWidget } from './kpi-group';
import { KpiWidget } from './kpi';
import { registerWidget } from './registry';
import { WindowNoteWidget } from './window-note';

/** Built-in widget types. Add new widgets here (see portal/README.md). */
registerWidget('kpi', KpiWidget);
registerWidget('kpi-group', KpiGroupWidget);
registerWidget('compare-bars', CompareBarsWidget);
registerWidget('funnel', FunnelWidget);
registerWidget('creatives-table', CreativesTableWidget);
registerWidget('window-note', WindowNoteWidget);

export { getWidget, registerWidget } from './registry';
export type { WidgetProps } from './registry';
