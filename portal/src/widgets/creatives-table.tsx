import type { CreativeRow } from '../api/types';
import { formatMetric, type MetricFormat } from '../format';
import { dictionaries } from '../i18n';
import type { WidgetProps } from './registry';
import { WidgetFrame } from './shared';

export type CreativeColumn = keyof CreativeRow;

export interface CreativesTableOptions {
  columns: CreativeColumn[];
  limit?: number;
}

const COLUMN_FORMAT: Record<Exclude<CreativeColumn, 'ad_id'>, MetricFormat> = {
  orders: 'count',
  delivered: 'count',
  delivery_rate: 'percent',
  delivered_value: 'money',
};

export function CreativesTableWidget({ data, instance, lang }: WidgetProps<CreativesTableOptions>) {
  const t = dictionaries[lang];
  const { columns, limit } = instance.options;
  const rows = limit === undefined ? data.creatives : data.creatives.slice(0, limit);
  return (
    <WidgetFrame instance={instance} lang={lang}>
      {rows.length === 0 ? (
        <p className="widget-empty">{t.creatives.empty}</p>
      ) : (
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                {columns.map((column) => (
                  <th key={column} scope="col" data-numeric={column !== 'ad_id' ? '' : undefined}>
                    {t.creatives.columns[column]}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.ad_id}>
                  {columns.map((column) =>
                    column === 'ad_id' ? (
                      <th key={column} scope="row">
                        {row.ad_id}
                      </th>
                    ) : (
                      <td key={column} data-numeric="">
                        {formatMetric(row[column], COLUMN_FORMAT[column], lang)}
                      </td>
                    ),
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </WidgetFrame>
  );
}
