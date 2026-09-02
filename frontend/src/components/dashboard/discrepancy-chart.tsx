"use client";

import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  LabelList,
  Tooltip as RechartsTooltip,
  XAxis,
  YAxis,
} from "recharts";

import { ChartContainer, type ChartConfig } from "@/components/ui/chart";
import {
  DISCREPANCY_TYPE_ORDER,
  formatCount,
  formatCurrency,
  typeColorVar,
  typeLabel,
} from "@/lib/discrepancy-types";

// `value` comes back as a JSON string (decimal precision), not a number --
// see the backend's `app.routers.reconcile` module docstring.
export type ByTypeRow = { type: string; count: number; value: string };

type ChartDatum = {
  type: string;
  label: string;
  count: number;
  value: number;
};

function buildChartData(byType: ByTypeRow[]): ChartDatum[] {
  const byKey = new Map(byType.map((row) => [row.type, row]));
  return DISCREPANCY_TYPE_ORDER.filter((type) => byKey.has(type)).map((type) => {
    const row = byKey.get(type)!;
    return { type, label: typeLabel(type), count: row.count, value: Number(row.value) };
  });
}

function ChartTooltipContent({
  active,
  payload,
}: {
  active?: boolean;
  payload?: Array<{ payload: ChartDatum }>;
}) {
  if (!active || !payload?.length) return null;
  const datum = payload[0].payload;

  return (
    <div className="grid min-w-40 gap-1.5 rounded-lg border border-border/50 bg-background px-2.5 py-1.5 text-xs shadow-xl">
      <div className="flex items-center gap-1.5 font-medium text-foreground">
        <span
          aria-hidden="true"
          className="h-2 w-2 shrink-0 rounded-full"
          style={{ backgroundColor: typeColorVar(datum.type) }}
        />
        {datum.label}
      </div>
      <div className="flex items-center justify-between gap-4">
        <span className="text-muted-foreground">Count</span>
        <span className="font-mono font-medium text-foreground tabular-nums">
          {formatCount(datum.count)}
        </span>
      </div>
      <div className="flex items-center justify-between gap-4">
        <span className="text-muted-foreground">Value</span>
        <span className="font-mono font-medium text-foreground tabular-nums">
          {formatCurrency(datum.value)}
        </span>
      </div>
    </div>
  );
}

// ChartContainer requires a config object; the color per bar is supplied
// per-datum below (see `typeColorVar`), not through this config, since the
// same color mapping must also drive the drill-down table's badges outside
// the chart's own scope.
const chartConfig = {
  count: { label: "Discrepancies" },
} satisfies ChartConfig;

/**
 * Horizontal bar chart of discrepancy count by type (dataviz skill:
 * categorical color per nominal category, since each bar's identity is
 * cross-referenced against the drill-down table's badges below). Axis
 * labels carry identity directly, so no separate legend is needed.
 */
export function DiscrepancyChart({ byType }: { byType: ByTypeRow[] }) {
  const data = buildChartData(byType);
  const height = Math.max(240, data.length * 40 + 40);

  if (data.length === 0) {
    return (
      <p className="text-muted-foreground text-sm">
        No discrepancy data for this run.
      </p>
    );
  }

  return (
    <ChartContainer
      config={chartConfig}
      className="aspect-auto w-full"
      style={{ height }}
    >
      <BarChart data={data} layout="vertical" margin={{ top: 4, right: 32, bottom: 4, left: 4 }}>
        <CartesianGrid horizontal={false} stroke="var(--border)" />
        <XAxis
          type="number"
          allowDecimals={false}
          tickLine={false}
          axisLine={false}
        />
        <YAxis
          type="category"
          dataKey="label"
          width={160}
          tickLine={false}
          axisLine={false}
        />
        <RechartsTooltip
          content={<ChartTooltipContent />}
          cursor={{ fill: "var(--muted)" }}
        />
        <Bar dataKey="count" radius={[0, 4, 4, 0]} barSize={20} isAnimationActive={false}>
          {data.map((datum) => (
            <Cell key={datum.type} fill={typeColorVar(datum.type)} />
          ))}
          <LabelList
            dataKey="count"
            position="right"
            formatter={(value: unknown) =>
              typeof value === "number" ? formatCount(value) : String(value ?? "")
            }
            style={{ fill: "var(--muted-foreground)", fontSize: 12 }}
          />
        </Bar>
      </BarChart>
    </ChartContainer>
  );
}
