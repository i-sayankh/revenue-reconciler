"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import type { ByTypeRow } from "@/components/dashboard/discrepancy-chart";
import { DiscrepancyChart } from "@/components/dashboard/discrepancy-chart";
import type { Discrepancy } from "@/components/dashboard/discrepancy-table";
import { DiscrepancyTable } from "@/components/dashboard/discrepancy-table";
import type { RunSummary } from "@/components/dashboard/stat-cards";
import { StatCards } from "@/components/dashboard/stat-cards";
import { ApiError, fetchApi } from "@/lib/api";

const PAGE_SIZE = 20;
const SEARCH_DEBOUNCE_MS = 300;

type LatestRunResponse = {
  run: RunSummary;
  by_type: ByTypeRow[];
};

type DiscrepanciesResponse = {
  total: number;
  page: number;
  page_size: number;
  results: Discrepancy[];
};

export default function DashboardPage() {
  const [run, setRun] = useState<RunSummary | null>(null);
  const [byType, setByType] = useState<ByTypeRow[]>([]);
  const [isLoadingRun, setIsLoadingRun] = useState(true);
  const [hasRun, setHasRun] = useState(true);

  const [typeFilter, setTypeFilter] = useState("all");
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const [results, setResults] = useState<Discrepancy[]>([]);
  const [total, setTotal] = useState(0);
  const [isLoadingTable, setIsLoadingTable] = useState(false);

  // Debounce the free-text search box before it hits the API.
  useEffect(() => {
    const timeout = setTimeout(() => {
      setSearch(searchInput);
      setPage(1);
    }, SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timeout);
  }, [searchInput]);

  useEffect(() => {
    let isMounted = true;

    async function loadRun() {
      setIsLoadingRun(true);
      try {
        const data: LatestRunResponse = await fetchApi("/api/reconcile/runs/latest");
        if (!isMounted) return;
        setRun(data.run);
        setByType(data.by_type);
        setHasRun(true);
      } catch (error) {
        if (!isMounted) return;
        if (error instanceof ApiError && error.status === 404) {
          setHasRun(false);
        } else {
          const message =
            error instanceof ApiError ? error.message : "Failed to load the reconciliation run.";
          toast.error(message);
        }
      } finally {
        if (isMounted) setIsLoadingRun(false);
      }
    }

    loadRun();
    return () => {
      isMounted = false;
    };
  }, []);

  useEffect(() => {
    if (!hasRun) {
      return;
    }

    let isMounted = true;

    async function loadDiscrepancies() {
      setIsLoadingTable(true);
      try {
        const params = new URLSearchParams();
        if (typeFilter !== "all") params.set("type", typeFilter);
        if (search) params.set("q", search);
        params.set("page", String(page));
        params.set("page_size", String(PAGE_SIZE));

        const data: DiscrepanciesResponse = await fetchApi(
          `/api/discrepancies?${params.toString()}`
        );
        if (!isMounted) return;
        setResults(data.results);
        setTotal(data.total);
      } catch (error) {
        if (!isMounted) return;
        const message =
          error instanceof ApiError ? error.message : "Failed to load discrepancies.";
        toast.error(message);
      } finally {
        if (isMounted) setIsLoadingTable(false);
      }
    }

    loadDiscrepancies();
    return () => {
      isMounted = false;
    };
  }, [hasRun, typeFilter, search, page]);

  function handleRowClick(discrepancy: Discrepancy) {
    // TODO: open the discrepancy explanation panel (separate feature).
    void discrepancy;
  }

  if (isLoadingRun) {
    return (
      <div className="flex flex-1 items-center justify-center">
        <p className="text-muted-foreground text-sm">Loading…</p>
      </div>
    );
  }

  if (!hasRun || !run) {
    return (
      <div className="flex flex-1 items-center justify-center p-6">
        <Card className="w-full max-w-md">
          <CardHeader>
            <CardTitle>No reconciliation run yet</CardTitle>
            <CardDescription>
              Upload your orders and payments to run reconciliation before viewing the
              dashboard.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Button className="w-full" render={<Link href="/upload">Upload data</Link>} />
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="flex-1 space-y-6 p-6">
      <div className="space-y-1">
        <h1 className="text-xl font-semibold">Reconciliation dashboard</h1>
        <p className="text-muted-foreground text-sm">
          Results from your most recent reconciliation run.
        </p>
      </div>

      <StatCards run={run} />

      <Card>
        <CardHeader>
          <CardTitle>Discrepancies by type</CardTitle>
          <CardDescription>
            Count of orders and payments in each outcome bucket, including reconciled.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <DiscrepancyChart byType={byType} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Discrepancies</CardTitle>
          <CardDescription>
            Filter, search, and page through every discrepancy from this run.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <DiscrepancyTable
            results={results}
            total={total}
            page={page}
            pageSize={PAGE_SIZE}
            typeFilter={typeFilter}
            search={searchInput}
            isLoading={isLoadingTable}
            onTypeFilterChange={(type) => {
              setTypeFilter(type);
              setPage(1);
            }}
            onSearchChange={setSearchInput}
            onPageChange={setPage}
            onRowClick={handleRowClick}
          />
        </CardContent>
      </Card>
    </div>
  );
}
