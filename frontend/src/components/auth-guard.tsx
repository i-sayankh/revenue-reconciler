"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

import { Skeleton } from "@/components/ui/skeleton";
import { useSession } from "@/hooks/use-session";

/**
 * Client-side route guard for authenticated pages. Redirects to /login
 * when there's no active Supabase session. Not middleware-level protection,
 * but it does actually navigate away rather than just hiding UI.
 */
export function AuthGuard({ children }: { children: React.ReactNode }) {
  const { session, isLoading } = useSession();
  const router = useRouter();

  useEffect(() => {
    if (!isLoading && !session) {
      router.replace("/login");
    }
  }, [isLoading, session, router]);

  if (isLoading || !session) {
    return (
      <div className="flex-1 space-y-6 p-6">
        <div className="space-y-2">
          <Skeleton className="h-6 w-48" />
          <Skeleton className="h-4 w-72" />
        </div>
        <Skeleton className="h-40 w-full" />
      </div>
    );
  }

  return <>{children}</>;
}
