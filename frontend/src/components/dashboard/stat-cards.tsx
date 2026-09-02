import {
  Card,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { formatCount, formatCurrency } from "@/lib/discrepancy-types";

export type RunSummary = {
  orders_count: number;
  payments_count: number;
  total_reconciled_value: string;
  total_disputed_value: string;
  money_at_risk: string;
};

function StatCard({ label, value }: { label: string; value: string }) {
  return (
    <Card>
      <CardHeader>
        <CardDescription>{label}</CardDescription>
        {/* Hero-figure styling: proportional figures, not tabular-nums --
            this is a standalone display number, not a table column. */}
        <CardTitle className="text-2xl font-semibold">{value}</CardTitle>
      </CardHeader>
    </Card>
  );
}

export function StatCards({ run }: { run: RunSummary }) {
  return (
    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
      <StatCard label="Total orders" value={formatCount(run.orders_count)} />
      <StatCard label="Total payments" value={formatCount(run.payments_count)} />
      <StatCard
        label="Value reconciled"
        value={formatCurrency(Number(run.total_reconciled_value))}
      />
      <StatCard
        label="Value in dispute"
        value={formatCurrency(Number(run.total_disputed_value))}
      />
      <StatCard
        label="Money at risk"
        value={formatCurrency(Number(run.money_at_risk))}
      />
    </div>
  );
}
