import { supabase } from "@/lib/supabase";

const apiUrl = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/**
 * Calls the backend API, attaching the current Supabase session's access
 * token as `Authorization: Bearer <token>` when one is available.
 *
 * Kept deliberately simple and stable: later features call the backend
 * through this helper rather than building their own fetch wrappers.
 */
export async function fetchApi(path: string, options: RequestInit = {}) {
  const {
    data: { session },
  } = await supabase.auth.getSession();

  const headers = new Headers(options.headers);
  headers.set("Accept", "application/json");
  if (
    options.body &&
    !(options.body instanceof FormData) &&
    !headers.has("Content-Type")
  ) {
    headers.set("Content-Type", "application/json");
  }
  if (session?.access_token) {
    headers.set("Authorization", `Bearer ${session.access_token}`);
  }

  const response = await fetch(`${apiUrl}${path}`, {
    ...options,
    headers,
  });

  if (!response.ok) {
    let message = response.statusText || "Request failed.";
    try {
      const body = await response.clone().json();
      message = body?.detail ?? body?.message ?? message;
    } catch {
      // Response body wasn't JSON — fall back to the status text.
    }
    throw new ApiError(message, response.status);
  }

  if (response.status === 204) {
    return null;
  }

  return response.json();
}
