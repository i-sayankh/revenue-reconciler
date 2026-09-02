"use client";

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { TypeBadge } from "@/components/dashboard/type-badge";
import { FILTERABLE_DISCREPANCY_TYPES, typeLabel } from "@/lib/discrepancy-types";

export type Discrepancy = {
  id: string;
  type: string;
  order_id: string | null;
  payment_ref: string | null;
  order_amount: string | null;
  payment_amount: string | null;
  currency_order: string | null;
  currency_payment: string | null;
  difference: string | null;
  // The backend serializes this as an arbitrary JSON object (always
  // includes a human-readable `reason`, e.g. "order net_amount 47.55 vs
  // payment amount 45.00 (diff 2.55)"), never a plain string.
  detail: { reason?: string; [key: string]: unknown } | null;
};

function formatAmount(amount: string | null, currency: string | null): string {
  if (amount === null) return "—";
  const value = Number(amount);
  return currency ? `${value.toFixed(2)} ${currency}` : value.toFixed(2);
}

function formatDetail(detail: Discrepancy["detail"]): string {
  if (!detail) return "—";
  if (typeof detail.reason === "string") return detail.reason;
  return "—";
}

export function DiscrepancyTable({
  results,
  total,
  page,
  pageSize,
  typeFilter,
  search,
  isLoading,
  onTypeFilterChange,
  onSearchChange,
  onPageChange,
  onRowClick,
}: {
  results: Discrepancy[];
  total: number;
  page: number;
  pageSize: number;
  typeFilter: string;
  search: string;
  isLoading: boolean;
  onTypeFilterChange: (type: string) => void;
  onSearchChange: (q: string) => void;
  onPageChange: (page: number) => void;
  onRowClick: (discrepancy: Discrepancy) => void;
}) {
  const pageCount = Math.max(1, Math.ceil(total / pageSize));

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
        <Select
          value={typeFilter}
          onValueChange={(value) => onTypeFilterChange(value ?? "all")}
        >
          <SelectTrigger className="w-full sm:w-56">
            <SelectValue placeholder="All types" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All types</SelectItem>
            {FILTERABLE_DISCREPANCY_TYPES.map((type) => (
              <SelectItem key={type} value={type}>
                {typeLabel(type)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Input
          placeholder="Search order ID or payment reference"
          value={search}
          onChange={(event) => onSearchChange(event.target.value)}
          className="sm:max-w-sm"
        />
      </div>

      <div className="overflow-hidden rounded-lg border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Type</TableHead>
              <TableHead>Order ID</TableHead>
              <TableHead>Payment ref</TableHead>
              <TableHead>Order amount</TableHead>
              <TableHead>Payment amount</TableHead>
              <TableHead>Detail</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoading ? (
              Array.from({ length: 5 }).map((_, index) => (
                <TableRow key={`skeleton-row-${index}`}>
                  <TableCell colSpan={6} className="py-3">
                    <Skeleton className="h-5 w-full" />
                  </TableCell>
                </TableRow>
              ))
            ) : results.length === 0 ? (
              <TableRow>
                <TableCell colSpan={6} className="py-8 text-center text-muted-foreground">
                  No discrepancies match these filters.
                </TableCell>
              </TableRow>
            ) : (
              results.map((row) => (
                <TableRow
                  key={row.id}
                  className="cursor-pointer"
                  data-discrepancy-id={row.id}
                  onClick={() => onRowClick(row)}
                >
                  <TableCell>
                    <TypeBadge type={row.type} />
                  </TableCell>
                  <TableCell className="font-mono text-xs tabular-nums">
                    {row.order_id ?? "—"}
                  </TableCell>
                  <TableCell className="font-mono text-xs tabular-nums">
                    {row.payment_ref ?? "—"}
                  </TableCell>
                  <TableCell className="tabular-nums">
                    {formatAmount(row.order_amount, row.currency_order)}
                  </TableCell>
                  <TableCell className="tabular-nums">
                    {formatAmount(row.payment_amount, row.currency_payment)}
                  </TableCell>
                  <TableCell
                    className="max-w-xs truncate text-muted-foreground"
                    title={formatDetail(row.detail)}
                  >
                    {formatDetail(row.detail)}
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2 text-sm text-muted-foreground">
        <span>
          {total === 0
            ? "0 results"
            : `Page ${page} of ${pageCount} — ${total} result${total === 1 ? "" : "s"}`}
        </span>
        <div className="flex gap-2">
          <Button
            variant="outline"
            size="sm"
            disabled={page <= 1 || isLoading}
            onClick={() => onPageChange(page - 1)}
          >
            Previous
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={page >= pageCount || isLoading}
            onClick={() => onPageChange(page + 1)}
          >
            Next
          </Button>
        </div>
      </div>
    </div>
  );
}
