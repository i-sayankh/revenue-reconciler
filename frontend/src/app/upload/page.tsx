"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { ApiError, fetchApi } from "@/lib/api";

type FileSlotKey = "orders" | "payments";

const GENERIC_FAILURE_MESSAGE =
  "Something went wrong while reconciling your files. Please try again.";

function isCsvFile(file: File): boolean {
  return file.name.toLowerCase().endsWith(".csv");
}

function FileSlot({
  title,
  description,
  file,
  error,
  disabled,
  onChange,
}: {
  title: string;
  description: string;
  file: File | null;
  error: string | null;
  disabled: boolean;
  onChange: (file: File | null, error: string | null) => void;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-2">
        <Input
          type="file"
          accept=".csv,text/csv"
          disabled={disabled}
          aria-invalid={error ? true : undefined}
          onChange={(event) => {
            const selected = event.target.files?.[0] ?? null;

            if (!selected) {
              onChange(null, null);
              return;
            }

            if (!isCsvFile(selected)) {
              event.target.value = "";
              const message = "Please select a .csv file.";
              onChange(null, message);
              toast.error(`${title}: ${message}`);
              return;
            }

            onChange(selected, null);
          }}
        />
        {error ? <p className="text-destructive text-sm">{error}</p> : null}
        {!error && file ? (
          <p className="text-muted-foreground text-sm">{file.name} selected.</p>
        ) : null}
      </CardContent>
    </Card>
  );
}

export default function UploadPage() {
  const router = useRouter();

  const [ordersFile, setOrdersFile] = useState<File | null>(null);
  const [paymentsFile, setPaymentsFile] = useState<File | null>(null);
  const [errors, setErrors] = useState<Record<FileSlotKey, string | null>>({
    orders: null,
    payments: null,
  });
  const [isSubmitting, setIsSubmitting] = useState(false);

  function setFile(key: FileSlotKey, file: File | null, error: string | null) {
    setErrors((prev) => ({ ...prev, [key]: error }));
    if (key === "orders") {
      setOrdersFile(file);
    } else {
      setPaymentsFile(file);
    }
  }

  async function handleRunReconciliation() {
    if (!ordersFile || !paymentsFile) {
      setErrors({
        orders: ordersFile ? null : "Select the orders CSV file.",
        payments: paymentsFile ? null : "Select the payments CSV file.",
      });
      return;
    }

    setErrors({ orders: null, payments: null });
    setIsSubmitting(true);

    try {
      try {
        const ordersForm = new FormData();
        ordersForm.append("file", ordersFile);
        await fetchApi("/api/ingest/orders", {
          method: "POST",
          body: ordersForm,
        });
      } catch (error) {
        const message =
          error instanceof ApiError
            ? error.message
            : "Failed to upload the orders file.";
        setErrors((prev) => ({ ...prev, orders: message }));
        throw new Error(message);
      }

      try {
        const paymentsForm = new FormData();
        paymentsForm.append("file", paymentsFile);
        await fetchApi("/api/ingest/payments", {
          method: "POST",
          body: paymentsForm,
        });
      } catch (error) {
        const message =
          error instanceof ApiError
            ? error.message
            : "Failed to upload the payments file.";
        setErrors((prev) => ({ ...prev, payments: message }));
        throw new Error(message);
      }

      await fetchApi("/api/reconcile/run", { method: "POST" });

      toast.success("Reconciliation complete.");
      router.push("/dashboard");
    } catch (error) {
      const message = error instanceof Error ? error.message : GENERIC_FAILURE_MESSAGE;
      toast.error(message);
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <div className="flex flex-1 items-center justify-center p-6">
      <div className="w-full max-w-2xl space-y-6">
        <div className="space-y-1 text-center">
          <h1 className="text-xl font-semibold">Upload your data</h1>
          <p className="text-muted-foreground text-sm">
            Upload your orders and payments CSV files to run reconciliation.
          </p>
        </div>

        <div className="grid gap-4 sm:grid-cols-2">
          <FileSlot
            title="Orders CSV"
            description="The exported list of orders."
            file={ordersFile}
            error={errors.orders}
            disabled={isSubmitting}
            onChange={(file, error) => setFile("orders", file, error)}
          />
          <FileSlot
            title="Payments CSV"
            description="The exported list of payments."
            file={paymentsFile}
            error={errors.payments}
            disabled={isSubmitting}
            onChange={(file, error) => setFile("payments", file, error)}
          />
        </div>

        <Button
          className="w-full"
          disabled={isSubmitting}
          onClick={handleRunReconciliation}
        >
          {isSubmitting ? "Running reconciliation…" : "Run reconciliation"}
        </Button>
      </div>
    </div>
  );
}
