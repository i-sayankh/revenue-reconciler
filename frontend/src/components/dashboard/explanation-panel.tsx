"use client";

import { useEffect, useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import type { Discrepancy } from "@/components/dashboard/discrepancy-table";
import { TypeBadge } from "@/components/dashboard/type-badge";
import { ApiError, fetchApi } from "@/lib/api";

type Explanation = {
  summary: string;
  likely_cause: string;
  recommended_action: string;
  confidence: "low" | "medium" | "high";
};

type ExplainResponse = {
  explanation: Explanation;
  explained_at: string | null;
};

function formatAmount(amount: string | null, currency: string | null): string {
  if (amount === null) return "—";
  const value = Number(amount);
  return currency ? `${value.toFixed(2)} ${currency}` : value.toFixed(2);
}

function DetailRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-4 text-sm">
      <span className="text-muted-foreground">{label}</span>
      <span className="text-right font-mono text-xs tabular-nums">{value}</span>
    </div>
  );
}

/**
 * Side panel showing one discrepancy's details plus its LLM-generated
 * explanation.
 *
 * The explanation fetch is intentionally lazy and self-contained: it only
 * fires once the panel is open for a given discrepancy (never preloaded per
 * row), and its loading/error state lives entirely inside this component so
 * a slow or failed explain call can never block or break the rest of the
 * dashboard.
 */
type LoadState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "success"; explanation: Explanation };

export function ExplanationPanel({
  discrepancy,
  onOpenChange,
}: {
  discrepancy: Discrepancy | null;
  onOpenChange: (open: boolean) => void;
}) {
  const [state, setState] = useState<LoadState>({ status: "idle" });
  // Bumping this re-runs the fetch effect below -- used by the retry button.
  const [attempt, setAttempt] = useState(0);

  const discrepancyId = discrepancy?.id ?? null;

  useEffect(() => {
    let isMounted = true;

    async function loadExplanation() {
      if (!discrepancyId) {
        setState({ status: "idle" });
        return;
      }
      setState({ status: "loading" });
      try {
        const data: ExplainResponse = await fetchApi(
          `/api/discrepancies/${discrepancyId}/explain`,
          { method: "POST" }
        );
        if (!isMounted) return;
        setState({ status: "success", explanation: data.explanation });
      } catch (err) {
        if (!isMounted) return;
        const message =
          err instanceof ApiError
            ? err.message
            : "Couldn't reach the server to load this explanation.";
        setState({ status: "error", message });
      }
    }

    loadExplanation();
    return () => {
      isMounted = false;
    };
  }, [discrepancyId, attempt]);

  const isLoading = state.status === "loading";
  const error = state.status === "error" ? state.message : null;
  const explanation = state.status === "success" ? state.explanation : null;

  return (
    <Sheet open={discrepancy !== null} onOpenChange={onOpenChange}>
      <SheetContent>
        {discrepancy && (
          <>
            <SheetHeader>
              <SheetTitle className="flex items-center gap-2">
                <TypeBadge type={discrepancy.type} />
              </SheetTitle>
              <SheetDescription>
                {discrepancy.order_id ?? discrepancy.payment_ref ?? discrepancy.id}
              </SheetDescription>
            </SheetHeader>

            <div className="space-y-4 overflow-y-auto px-4">
              <div className="space-y-1.5 rounded-lg border p-3">
                <DetailRow label="Order ID" value={discrepancy.order_id ?? "—"} />
                <DetailRow label="Payment ref" value={discrepancy.payment_ref ?? "—"} />
                <DetailRow
                  label="Order amount"
                  value={formatAmount(discrepancy.order_amount, discrepancy.currency_order)}
                />
                <DetailRow
                  label="Payment amount"
                  value={formatAmount(discrepancy.payment_amount, discrepancy.currency_payment)}
                />
                {discrepancy.difference !== null && (
                  <DetailRow label="Difference" value={discrepancy.difference} />
                )}
              </div>
              {discrepancy.detail?.reason && (
                <p className="text-sm text-muted-foreground">{discrepancy.detail.reason}</p>
              )}

              <div className="space-y-3">
                <h3 className="text-sm font-medium">Explanation</h3>

                {isLoading && (
                  <div className="space-y-2" data-testid="explanation-loading">
                    <Skeleton className="h-4 w-full" />
                    <Skeleton className="h-4 w-5/6" />
                    <Skeleton className="h-4 w-3/4" />
                  </div>
                )}

                {!isLoading && error && (
                  <Alert variant="destructive" data-testid="explanation-error">
                    <AlertTitle>Couldn&apos;t load explanation</AlertTitle>
                    <AlertDescription>
                      <p>{error}</p>
                      <Button
                        variant="outline"
                        size="sm"
                        className="mt-2"
                        onClick={() => setAttempt((n) => n + 1)}
                      >
                        Retry
                      </Button>
                    </AlertDescription>
                  </Alert>
                )}

                {!isLoading && !error && explanation && (
                  <div className="space-y-3" data-testid="explanation-content">
                    <div className="flex items-center gap-2">
                      <Badge
                        variant={explanation.confidence === "low" ? "outline" : "secondary"}
                        className="font-normal capitalize"
                      >
                        {explanation.confidence} confidence
                      </Badge>
                    </div>
                    <p className="text-sm">{explanation.summary}</p>
                    <div className="space-y-1">
                      <h4 className="text-xs font-medium text-muted-foreground">Likely cause</h4>
                      <p className="text-sm">{explanation.likely_cause}</p>
                    </div>
                    <div className="space-y-1">
                      <h4 className="text-xs font-medium text-muted-foreground">
                        Recommended action
                      </h4>
                      <p className="text-sm">{explanation.recommended_action}</p>
                    </div>
                  </div>
                )}
              </div>
            </div>
          </>
        )}
      </SheetContent>
    </Sheet>
  );
}
